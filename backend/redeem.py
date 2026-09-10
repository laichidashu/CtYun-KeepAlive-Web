# -*- coding: utf-8 -*-
"""
兑换服务（对应 C# RedeemService.cs + RedeemSchedulePolicy.cs）。
通道 A：selforder 奖励/积分拉取 + placeOrder 下单（复用 CtYunApi 签名头）。
"""
import json
import re
import threading
import time
from datetime import date, datetime, timedelta

import ctyun_api
import keepalive
import logs
import store
from store import ConfigStore, G, Paths


class RedeemConfig:
    """兑换配置。JSON 名与 pc_hang_task.py 逐字一致（C#/Python 之间的硬契约）。"""

    def __init__(self):
        self.enabled = False
        self.desktop_id = ""       # 脚本侧 int(...) 转换，保持 string
        self.prod_id = 0
        self.prod_name = ""
        self.prod_type = ""
        self.cost_points = 0
        self.max_redeem_times = 0  # 0 = 按积分尽量兑换
        self.last_redeem_date = "" # "YYYY-MM-DD"
        self.schedule_type = "daily"   # daily | interval_days | monthly_days
        self.interval_days = 1
        self.monthly_days = []     # -1 表示月末

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "desktopId": self.desktop_id,
            "prodId": self.prod_id,
            "prodName": self.prod_name,
            "prodType": self.prod_type,
            "costPoints": self.cost_points,
            "maxRedeemTimes": self.max_redeem_times,
            "lastRedeemDate": self.last_redeem_date,
            "scheduleType": self.schedule_type,
            "intervalDays": self.interval_days,
            "monthlyDays": self.monthly_days,
        }

    @classmethod
    def from_dict(cls, d):
        c = cls()
        c.enabled = bool(d.get("enabled", False))
        c.desktop_id = str(d.get("desktopId", "") or "")
        try:
            c.prod_id = int(d.get("prodId", 0))
        except (TypeError, ValueError):
            c.prod_id = 0
        c.prod_name = d.get("prodName", "") or ""
        c.prod_type = d.get("prodType", "") or ""
        try:
            c.cost_points = int(d.get("costPoints", 0))
        except (TypeError, ValueError):
            c.cost_points = 0
        try:
            c.max_redeem_times = int(d.get("maxRedeemTimes", 0))
        except (TypeError, ValueError):
            c.max_redeem_times = 0
        c.last_redeem_date = d.get("lastRedeemDate", "") or ""
        c.schedule_type = d.get("scheduleType", "daily") or "daily"
        try:
            c.interval_days = int(d.get("intervalDays", 1))
        except (TypeError, ValueError):
            c.interval_days = 1
        c.monthly_days = [int(x) for x in (d.get("monthlyDays") or []) if isinstance(x, (int, str)) and str(x).lstrip("-").isdigit()]
        return c


def load_redeem_config() -> RedeemConfig:
    return ConfigStore.load(Paths.redeem_config_path, RedeemConfig.from_dict, lambda: RedeemConfig())


def save_redeem_config(cfg: RedeemConfig):
    ConfigStore.save(Paths.redeem_config_path, cfg.to_dict)


# ---------- 调度策略 ----------

def evaluate_policy(cfg: RedeemConfig, today: date, manual: bool = False):
    """返回 (should, reason)。manual=True 时不做兑换日限制（保留当日防重复兑换）。"""
    if cfg is None:
        return False, "兑换配置为空，跳过。"
    stype = (cfg.schedule_type or "daily").strip()
    last = (cfg.last_redeem_date or "").strip()
    today_str = today.strftime("%Y-%m-%d")
    if last == today_str:
        return False, "今天(%s)已兑换过，跳过。" % today_str
    if manual:
        return True, "手动执行，跳过兑换日限制。"
    if stype == "daily":
        return True, "每日兑换策略，允许执行。"
    if stype == "interval_days":
        n = cfg.interval_days if cfg.interval_days >= 1 else 1
        if not last:
            return True, "间隔兑换策略首次执行。"
        try:
            last_day = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            return True, "上次兑换日期格式异常，允许执行。"
        passed = (today - last_day).days
        if passed >= n:
            return True, "已间隔 %d 天，满足每隔 %d 天兑换。" % (passed, n)
        return False, "距上次仅 %d 天，未到每隔 %d 天。" % (passed, n)
    if stype == "monthly_days":
        days = cfg.monthly_days or []
        allow_end = False
        allowed = set()
        for d in days:
            if d == -1:
                allow_end = True
                continue
            if 1 <= d <= 31:
                allowed.add(d)
        if not allowed and not allow_end:
            return False, "每月兑换日期为空，跳过。"
        import calendar
        last_dom = calendar.monthrange(today.year, today.month)[1]
        if allow_end and today.day == last_dom:
            return True, "今天是 %d 号（本月最后一天），命中每月兑换日。" % today.day
        if today.day in allowed:
            return True, "今天是 %d 号，命中每月兑换日。" % today.day
        disp = sorted(allowed)
        if allow_end:
            disp.append(-1)
        return False, "今天是 %d 号，不在每月兑换日 [%s] 中。" % (today.day, ",".join(str(x) for x in disp))
    return True, "未知策略，按每日策略执行。"


# ---------- 通道 A 服务 ----------

def _login_first():
    """挑选第一个可用账号并完成登录；无账号/登录失败返回 None。"""
    accounts = G.config.accounts if G.config else []
    if not accounts:
        return None
    account = accounts[0]
    api = ctyun_api.CtYunApi(account.device_code)
    if not api.login(account.user, account.password):
        return None
    return api


def _pick_account_for_desktop(desktop_id):
    """根据 desktopId 找到拥有该云电脑的账号（用谁的积分给谁扩容）；找不到返回 None。"""
    did = str(desktop_id or "").strip()
    if not did:
        return None
    with G._gate:
        for acc in (G.config.accounts if G.config else []):
            key = keepalive.normalize_key(acc.user)
            info = G.account_statuses.get(key)
            for d in (info.desktops if info else []):
                if str(d.get("desktopId", "")).strip() == did:
                    return acc
    return None


def get_rewards(api):
    """拉取可用奖励列表（跳过过期 series）。错误（含 40010）返回 None。"""
    url = "https://desk.ctyun.cn/selforder/api/selforder/prod/get?prodId=17000000&prodCode=POINTS"
    ok, code, msg, data = api._request_json("GET", url)
    if not ok:
        logs.fail("[兑换]", "selforder 奖励列表请求异常：" + msg)
        return None
    if code == 40010:
        G.channel_a_state = "LoginExpired"
        logs.fail("[兑换]", "selforder 接口登录态失效（code=40010），通道不可用")
        return None
    if code != 0:
        logs.fail("[兑换]", "selforder 奖励列表拉取失败：code=%s, msg=%s" % (code, msg))
        return None

    items = []
    for group in (data or []):
        for series in (group.get("series") or []):
            if series.get("expireDate"):
                continue  # 跳过过期 series
            for sku in (series.get("sku") or []):
                items.append({
                    "prodId": sku.get("prodId", 0),
                    "prodName": sku.get("prodName", ""),
                    "costPoints": sku.get("costPoints", 0),
                    "description": sku.get("description", ""),
                    "prodType": sku.get("prodType", ""),
                })
    return items


def get_points(api) -> int:
    """「使用1小时」任务当前进度（即 1 小时消耗积分）。错误/40010 返回 -1。"""
    url = "https://desk.ctyun.cn/selforder/api/marketing/userPoints/getTaskList"
    ok, code, msg, data = api._request_json("GET", url)
    if not ok:
        logs.fail("[兑换]", "积分任务列表请求异常：" + msg)
        return -1
    if code == 40010:
        G.channel_a_state = "LoginExpired"
        logs.fail("[兑换]", "selforder 接口登录态失效（code=40010），通道不可用")
        return -1
    if code != 0:
        logs.fail("[兑换]", "积分任务列表拉取失败：code=%s, msg=%s" % (code, msg))
        return -1
    for task in (data or []):
        if task.get("taskDefName") == "使用1小时":
            try:
                return int(task.get("currentProgress", -1))
            except (TypeError, ValueError):
                return -1
    return -1


# ---------- 平台任务预检（供定时任务调度层调用） ----------

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_task_list(api):
    """拉取平台积分任务原始列表；失败返回 None。"""
    url = "https://desk.ctyun.cn/selforder/api/marketing/userPoints/getTaskList"
    ok, code, msg, data = api._request_json("GET", url)
    if not ok:
        return None
    if code == 40010:
        G.channel_a_state = "LoginExpired"
        return None
    if code != 0:
        return None
    return data or []


def _infer_target(name):
    """从任务名称推断目标值（平台接口只返回进度、不返回目标值时的兜底）。

    实测样例：「使用1小时」进度单位为秒（3600 = 完成）、「与AI对话1次」= 1 次、
    「登录AI云电脑」= 1 次。返回 None 表示无法推断（保持三态无法判断）。
    """
    if not name:
        return None
    m = re.search(r"(\d+)\s*小时", name)
    if m:
        return int(m.group(1)) * 3600
    m = re.search(r"(\d+)\s*分钟", name)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*次", name)
    if m:
        return int(m.group(1))
    if "登录" in name:
        return 1
    return None


def _task_target(t):
    """单个任务的目标次数（limitProgress 等字段），无则尝试从名称推断。"""
    for k in ("limitProgress", "targetProgress", "taskLimit",
              "limit", "target", "maxProgress", "needProgress"):
        v = _to_int(t.get(k))
        if v is not None and v > 0:
            return v
    return _infer_target(str(t.get("taskDefName") or ""))


def _task_done_flag(t):
    """单个任务的完成判定：True=已完成 / False=未完成 / None=无法判断。"""
    # 1) 显式布尔完成标记
    for k in ("completed", "isComplete", "finished", "finish", "done", "received"):
        v = t.get(k)
        if isinstance(v, bool):
            return v
    # 2) 进度 vs 目标
    progress = _to_int(t.get("currentProgress"))
    if progress is None:
        return None
    target = _task_target(t)
    if target:
        return progress >= target
    # 3) 只有进度无目标：无法确认完成（调用方 fail-open）
    return None


# 平台官方任务名 → Web 展示名。
# 用户视角：「使用1小时」就是云电脑挂机（挂机就是为了把「使用1小时」进度刷满），
# 界面上统一显示为「云电脑挂机」。**仅用于展示**，任何判定/匹配一律使用平台原名。
TASK_DISPLAY_ALIAS = {
    "使用1小时": "云电脑挂机",
}


def display_task_name(raw) -> str:
    """把平台官方任务名映射为 Web 展示名（只影响显示，不影响进度判定）。"""
    name = str(raw or "").strip()
    if not name:
        return "未命名"
    for official, shown in TASK_DISPLAY_ALIAS.items():
        if official in name:
            return shown
    return name


def task_overview(api):
    """拉取平台全部积分任务的完成情况（供 Web 面板展示）。

    返回 (tasks, error)：
      tasks: [{"name", "progress", "limit", "done", "reward"}, ...]；done 同上三态。
      error: None 或错误描述字符串。
    """
    tasks = fetch_task_list(api)
    if tasks is None:
        return None, "平台任务列表拉取失败（登录态可能失效或接口异常）"
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        out.append({
            "name": display_task_name(t.get("taskDefName")),
            "progress": _to_int(t.get("currentProgress")),
            "limit": _task_target(t),
            "done": _task_done_flag(t),
            "reward": _to_int(t.get("integral") or t.get("reward") or t.get("points")),
        })
    return out, None


def probe_task_done(api, keyword):
    """探测名称含 keyword 的平台任务是否已完成。

    返回 (done, desc)：
      done=True   确认已完成；
      done=False  确认未完成；
      done=None   无法判断（调用方应 fail-open，照常执行任务）。
    desc 为全部任务的进度摘要，便于日志留痕。
    """
    tasks = fetch_task_list(api)
    if tasks is None:
        return None, "平台任务列表拉取失败"
    lines = []
    target = None
    for t in tasks:
        if not isinstance(t, dict):
            continue
        # 匹配一律用平台原名，展示用映射后的名称
        raw_name = str(t.get("taskDefName") or "未命名")
        progress = t.get("currentProgress")
        lines.append("%s:%s" % (display_task_name(raw_name), progress))
        if keyword and keyword in raw_name:
            target = t
    desc = "平台任务[" + "; ".join(lines) + "]" if lines else "平台任务列表为空"
    if target is None:
        return None, desc + "（未找到含「%s」的任务，无法判断）" % display_task_name(keyword)
    return _task_done_flag(target), desc


def execute(cfg: RedeemConfig, manual: bool = False):
    """执行兑换（通道 A）。返回 (ok, msg)。manual=True 为前端手动触发。"""
    if cfg is None:
        return False, "兑换配置为空"
    try:
        desktop_id = int(cfg.desktop_id)
    except (TypeError, ValueError):
        return False, "云电脑 ID 非法"

    # 兑换账号：优先用拥有目标云电脑的账号（用谁的积分给谁扩容），找不到回落第一个账号
    acc = _pick_account_for_desktop(cfg.desktop_id)
    if acc is None:
        accounts = G.config.accounts if G.config else []
        acc = accounts[0] if accounts else None
    if acc is None:
        return False, "无可用账号"
    api = ctyun_api.CtYunApi(acc.device_code)
    if not api.login(acc.user, acc.password):
        logs.fail("[兑换]", "兑换账号 %s 登录失败" % logs.mask_user(acc.user))
        return False, "兑换账号 %s 登录失败" % logs.mask_user(acc.user)
    logs.info("[兑换]", "兑换账号：%s（目标云电脑 %s）" % (logs.mask_user(acc.user), cfg.desktop_id))

    # ★ 双重校验：第 1 道 —— 调度策略门禁（手动执行跳过兑换日限制）
    should, reason = evaluate_policy(cfg, date.today(), manual=manual)
    if not should:
        return False, "兑换计划未执行：" + reason

    # 可用积分：getUserPoints 的「通用积分」（同名多行已求和）。
    # 注意：get_points() 是"使用1小时"任务进度，不是余额，不能用来判断可兑换次数。
    detail = get_user_points(api)
    points = detail.get("通用积分")
    if points is None:
        m = "无法读取通用积分余额（明细：%s）" % (json.dumps(detail, ensure_ascii=False) if detail else "空")
        logs.fail("[兑换]", m)
        return False, m
    cost = cfg.cost_points
    if cost <= 0:
        return False, "兑换积分成本非法"

    times = points // cost if cfg.max_redeem_times == 0 else min(points // cost, cfg.max_redeem_times)
    if times < 1:
        return False, "积分不足：当前 %d，需 %d/次" % (points, cost)

    body = {
        "busiChannel": "010",
        "orderType": 1,
        "pointType": 1,
        "points": cost * times,
        "sku": [{
            "execSort": i + 1,
            "prodId": cfg.prod_id,
            "prodType": cfg.prod_type,
            "attrs": [{"attrKey": "bindDesktopId", "attrVal": desktop_id}],
        } for i in range(times)],
    }
    url = "https://desk.ctyun.cn/selforder/api/selforder/paas/placeOrder"
    ok, code, msg, _ = api._request_json(
        "POST", url, data=json.dumps(body).encode("utf-8"), content_type="application/json")
    if not ok:
        logs.fail("[兑换]", "兑换失败：" + msg)
        return False, "兑换失败：" + msg

    if code == 0:
        cfg.last_redeem_date = date.today().strftime("%Y-%m-%d")
        save_redeem_config(cfg)
        logs.ok("[兑换]", "兑换成功：%s × %d，消耗 %d 积分" % (cfg.prod_name, times, cost * times))
        from keepalive import KeepAliveRestarter
        KeepAliveRestarter.schedule_restart(int(time.time()) + 120)
        return True, "兑换成功"
    if code == 40010:
        G.channel_a_state = "LoginExpired"
        m = "selforder 接口登录态失效（code=40010），通道不可用。请启用「云电脑挂机」任务走 Python 通道 B。"
        logs.fail("[兑换]", m)
        return False, m
    if code == 30010:
        logs.fail("[兑换]", "资源施工中，请稍后再试（code=30010）")
        return False, "资源施工中，请稍后再试（code=30010）"
    logs.fail("[兑换]", "兑换失败：code=%s, msg=%s" % (code, msg))
    return False, "兑换失败：code=%s, msg=%s" % (code, msg)


# ---------- 账号积分余额（get_user_points + 全账号刷新） ----------

_USER_POINTS_URL = "https://desk.ctyun.cn/selforder/api/marketing/userPoints/getUserPoints"
_points_gate = threading.Lock()
_points_refreshing = False


def get_user_points(api) -> dict:
    """拉取账号各类型积分余额。返回 {pointTypeName: points}；失败返回 {}。
    口径与官方 App 一致：余额 = willOutDate 非 true 的行（同名多行求和）；
    willOutDate=true 的行是"即将过期批次"拆分行（非独立积分），不计入余额，
    另存到 result["_expiring"] 供日志/展示提醒。"""
    ok, code, msg, data = api._request_json("GET", _USER_POINTS_URL)
    if not ok or code != 0:
        return {}
    # 原始返回落日志（排查积分对不上问题时直接看平台给的全部行）
    try:
        logs.info("[兑换]", "积分原始返回：" + json.dumps(data, ensure_ascii=False)[:800])
    except Exception:
        pass
    result = {}
    expiring = []
    for item in (data or []):
        if isinstance(item, dict) and item.get("pointTypeName"):
            name = str(item["pointTypeName"])
            try:
                val = int(item.get("points", 0))
            except (TypeError, ValueError):
                val = 0
            if item.get("willOutDate"):
                expiring.append({"name": name, "points": val,
                                 "outDateTime": item.get("outDateTime") or ""})
            else:
                result[name] = result.get(name, 0) + val
    if expiring:
        result["_expiring"] = expiring
        logs.info("[兑换]", "另有即将过期积分（不计入余额）：" + "；".join(
            "%s %d分（%s 过期）" % (e["name"], e["points"], e["outDateTime"] or "时间待定")
            for e in expiring))
    return result


def is_refreshing_points() -> bool:
    return _points_refreshing


def _points_worker(accounts):
    """逐账号登录并查询积分，写入 G.points_cache。"""
    global _points_refreshing
    try:
        for acc in accounts:
            key = acc.user
            try:
                api = ctyun_api.CtYunApi(acc.device_code)
                if not api.login(acc.user, acc.password):
                    with G._gate:
                        G.points_cache[key] = {
                            "points": None, "detail": {},
                            "checkedAt": int(time.time()), "error": "登录失败"}
                    logs.fail("积分", "[%s] 查询失败：登录失败" % logs.mask_user(key))
                    continue
                detail = get_user_points(api)
                generic = detail.get("通用积分")
                with G._gate:
                    G.points_cache[key] = {
                        "points": generic, "detail": detail,
                        "checkedAt": int(time.time()),
                        "error": "" if generic is not None else "未读取到通用积分"}
                logs.info("积分", "[%s] 通用积分：%s（%s）" % (
                    logs.mask_user(key),
                    generic if generic is not None else "未读取到",
                    "；".join("%s %s" % (k, v) for k, v in detail.items() if k != "_expiring") or "无明细"))
            except Exception as ex:
                with G._gate:
                    G.points_cache[key] = {
                        "points": None, "detail": {},
                        "checkedAt": int(time.time()), "error": str(ex)[:200]}
                logs.fail("积分", "[%s] 查询异常：%s" % (logs.mask_user(key), ex))
    finally:
        with _points_gate:
            _points_refreshing = False


def refresh_all_points() -> dict:
    """异步刷新全部账号的积分余额。返回 {started, msg, total}。"""
    global _points_refreshing
    with _points_gate:
        if _points_refreshing:
            return {"started": False, "msg": "积分刷新正在进行中，请稍候", "total": 0}
        accounts = list(G.config.accounts) if G.config else []
        if not accounts:
            return {"started": False, "msg": "暂无已配置账号", "total": 0}
        _points_refreshing = True
    threading.Thread(target=_points_worker, args=(accounts,),
                     name="points-refresh", daemon=True).start()
    return {"started": True, "msg": "积分刷新已启动（逐账号查询，进度见实时日志）", "total": len(accounts)}
