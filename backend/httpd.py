# -*- coding: utf-8 -*-
"""
Web 服务（对应 C# Program.cs Web 构建 + Endpoints/* 5 组端点）。
- ThreadingHTTPServer，每连接一线程
- 静态文件：frontend/（index.html 为默认页，兼容旧布局 CtYun/wwwroot）
- API 契约与 .NET 版逐字一致（前端零改动）
- SSE 日志流：token 允许走查询参数（EventSource 不能设头）
"""
import json
import os
import queue
import secrets
import string
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import envprobe
import keepalive
import logs
import mutex
import redeem
import scriptrunner
import sessions
from store import ConfigStore, G, Paths, is_container
from sessions import AdminSessionStore

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 前端静态目录：优先 frontend/（整合后），兼容旧布局 CtYun/wwwroot
WWWROOT = os.path.join(_BASE, "frontend")
if not os.path.isdir(WWWROOT):
    WWWROOT = os.path.join(_BASE, "CtYun", "wwwroot")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


# 全部账号平台任务统计的后台任务状态（登录较慢，走后台线程 + 前端轮询）
_platform_all_lock = threading.Lock()
_platform_all_state = {
    "running": False,
    "startedAt": 0.0,
    "finishedAt": 0.0,
    "ok": False,
    "msg": "",
    "accounts": [],                            # [{user, name, ok, msg, tasks}]
    "total": {"done": 0, "doing": 0, "todo": 0},
}

# 单账号平台任务查询结果缓存（用户点「平台任务 ▾」时刷新）。
# 与全量快照分开保存，避免污染全量统计的 total；仅供任务汇总判断平台是否达标。
_platform_user_lock = threading.Lock()
_platform_user_cache = {}                      # user → {"tasks": [...], "at": ts}

# 平台任务快照/缓存落盘：让「已完成（平台达标）」状态在服务重启后依然正确。
# 只信任**当天**的快照（平台任务按天重置，旧快照会误判）。
_PLATFORM_CACHE_FILE = "platform_tasks_cache.json"
_platform_cache_loaded = False


def _platform_cache_path() -> str:
    return os.path.join(Paths.data_dir, _PLATFORM_CACHE_FILE)


def _save_platform_cache():
    """把最近一次全量快照落盘（失败不影响主流程）。"""
    try:
        with _platform_all_lock:
            accounts = list(_platform_all_state.get("accounts") or [])
            finished = _platform_all_state.get("finishedAt") or 0
        if not accounts:
            return
        path = _platform_cache_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"finishedAt": finished, "accounts": accounts},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _ensure_platform_cache_loaded():
    """内存快照为空时，首次从磁盘恢复（仅在服务重启后生效一次）。"""
    global _platform_cache_loaded
    if _platform_cache_loaded:
        return
    _platform_cache_loaded = True
    with _platform_all_lock:
        if _platform_all_state.get("accounts"):
            return
    try:
        with open(_platform_cache_path(), "r", encoding="utf-8") as f:
            snap = json.load(f)
        accounts = snap.get("accounts") or []
        if accounts:
            with _platform_all_lock:
                _platform_all_state["accounts"] = accounts
                _platform_all_state["finishedAt"] = snap.get("finishedAt") or 0
    except Exception:
        pass


def _same_local_day(ts) -> bool:
    """时间戳是否与「现在」是同一个本地自然日。"""
    try:
        a = time.localtime(float(ts))
        b = time.localtime()
        return (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday)
    except Exception:
        return False


def _platform_all_worker():
    """逐个账号登录平台拉取全部积分任务，汇总完成/进行中/未完成数量。"""
    import ctyun_api
    try:
        accounts = list((G.config.accounts if G.config else []) or [])
    except Exception:
        accounts = []
    results = []
    total = {"done": 0, "doing": 0, "todo": 0}
    for acc in accounts:
        entry = {"user": acc.user, "name": acc.name or "", "ok": False, "msg": "", "tasks": []}
        try:
            api = ctyun_api.CtYunApi(acc.device_code)
            if not api.login(acc.user, acc.password):
                entry["msg"] = "平台登录失败"
            else:
                tasks, err = redeem.task_overview(api)
                if err:
                    entry["msg"] = err
                else:
                    entry["ok"] = True
                    entry["tasks"] = tasks
                    for t in tasks:
                        if t["done"] is True:
                            total["done"] += 1
                        elif t["progress"] is not None and t["progress"] > 0:
                            total["doing"] += 1
                        else:
                            total["todo"] += 1
        except Exception as ex:
            entry["msg"] = str(ex)
        results.append(entry)
        with _platform_all_lock:
            _platform_all_state["accounts"] = list(results)
            _platform_all_state["total"] = dict(total)
    with _platform_all_lock:
        _platform_all_state["running"] = False
        _platform_all_state["finishedAt"] = time.time()
        _platform_all_state["ok"] = any(a["ok"] for a in results)
    logs.info("任务", "[平台任务] 全量统计完成（%d 个账号）：已完成 %d / 进行中 %d / 未完成 %d"
              % (len(results), total["done"], total["doing"], total["todo"]))
    _save_platform_cache()


# 平台任务名 → 定时任务类型 的匹配关键字（匹配一律用平台原名）
_PLATFORM_TASK_KEYWORDS = {
    "pc_hang": ("使用1小时", "云电脑挂机"),
    "ai_chat": ("与AI对话", "对话"),
}


def _platform_done_lookup():
    """由「全量快照 + 单账号查询缓存」构造 {user: {jobType: done}}。

    用途：任务汇总里判断「该账号的平台任务是否今日已达标」——若已达标，
    定时任务即使今天还没到点执行，也不应显示「今日未执行」（与平台面板自相矛盾）。
    两处都没有数据的账号不返回（=未知），前端保持原状态显示。
    """
    _ensure_platform_cache_loaded()
    with _platform_all_lock:
        accounts = list(_platform_all_state.get("accounts") or [])
        finished = _platform_all_state.get("finishedAt") or 0
    if not _same_local_day(finished):
        accounts = []          # 旧快照（昨日及更早）不得用于判断「今日达标」
    with _platform_user_lock:
        cache = dict(_platform_user_cache)

    def _per_of(tasks):
        per = {}
        for t in (tasks or []):
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "")
            for jt, kws in _PLATFORM_TASK_KEYWORDS.items():
                if any(k in name for k in kws):
                    per[jt] = (t.get("done") is True)
        return per

    out = {}
    for acc in accounts:
        if not isinstance(acc, dict) or not acc.get("ok"):
            continue
        user = str(acc.get("user") or "")
        if not user:
            continue
        per = _per_of(acc.get("tasks"))
        if per:
            out[user] = per
    # 单账号查询缓存通常更新更近，覆盖同 user 的结果（同样只认当天）
    for user, item in cache.items():
        if not _same_local_day((item or {}).get("at")):
            continue
        per = _per_of((item or {}).get("tasks"))
        if per:
            out[str(user)] = per
    return out


# ---------- Program.cs 辅助函数移植 ----------

def first_not_empty(*values):
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return ""


def safe_name(value: str) -> str:
    src = value if value and value.strip() else "default"
    return "".join(ch if ch.isalnum() else "_" for ch in src)


def generate_random_string(length: int) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _restart_after_update(delay: float = 1.5):
    """更新完成后重启服务：给前端留出收包时间，再交给 updater 拉起新进程。"""
    time.sleep(delay)
    try:
        import updater
        updater.restart_service()
    except Exception as ex:
        logs.fail("系统", "自动重启失败，请手动重启服务：" + str(ex))
        return
    time.sleep(0.5)
    os._exit(0)


def resolve_device_code(account, data_dir: str) -> str:
    """设备码：账号自带 → devices/{safe}.txt 兜底文件（web_ + 32 随机字符）。"""
    if account.device_code and account.device_code.strip():
        return account.device_code.strip()
    devices_dir = os.path.join(data_dir, "devices")
    os.makedirs(devices_dir, exist_ok=True)
    device_code_path = os.path.join(devices_dir, safe_name(account.name or account.user) + ".txt")
    if not os.path.exists(device_code_path):
        with open(device_code_path, "w", encoding="utf-8") as f:
            f.write("web_" + generate_random_string(32))
    with open(device_code_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_accounts_from_environment():
    """无配置文件时从环境变量读取（APP_USER / APP_PASSWORD / APP_NAME / DEVICECODE）。"""
    from store import AppConfig, AccountConfig
    user = os.environ.get("APP_USER", "")
    password = os.environ.get("APP_PASSWORD", "")
    if not user.strip() or not password.strip():
        return None
    cfg = AppConfig()
    acc = AccountConfig()
    acc.name = os.environ.get("APP_NAME", "") or ""
    acc.user = user
    acc.password = password
    acc.device_code = os.environ.get("DEVICECODE", "") or ""
    cfg.accounts = [acc]
    return cfg


def upsert_account(account):
    accounts = G.config.accounts
    accounts[:] = [a for a in accounts if a.user != account.user]
    accounts.append(account)


# ---------- HTTP Handler ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CtYunKeepAlive/2.0-py"

    # ----- 基础输出 -----

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok_web(self, **kwargs):
        """WebResponseBase：仅输出非空字段（与 C# null 序列化等效）。"""
        out = {}
        if kwargs.get("status"):
            out["status"] = kwargs["status"]
        if kwargs.get("message"):
            out["message"] = kwargs["message"]
        if "success" in kwargs:
            out["success"] = kwargs["success"]
        if kwargs.get("msg"):
            out["msg"] = kwargs["msg"]
        self._json(out)

    def _unauthorized(self):
        self._json({"success": False, "msg": "未授权"}, status=401)

    def _authorize(self) -> bool:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = sessions.get_token_from_headers(self.headers, {k: v[0] for k, v in query.items()})
        if AdminSessionStore.validate(token):
            self._token = token
            return True
        return False

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None

    def log_message(self, fmt, *args):
        pass  # 关闭默认控制台访问日志（业务日志走 SSE 广播）

    # ----- 方法分发 -----

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            if method == "GET" and not path.startswith("/api/"):
                self._serve_static(path)
                return

            if path == "/api/login" and method == "POST":
                self._ep_login()
            elif path == "/api/logout" and method == "POST":
                self._ep_logout()
            elif path == "/api/change-password" and method == "POST":
                self._ep_change_password()
            elif path == "/api/accounts" and method == "GET":
                self._ep_accounts_list()
            elif path == "/api/accounts" and method == "POST":
                self._ep_accounts_add()
            elif path == "/api/accounts/verify" and method == "POST":
                self._ep_accounts_verify()
            elif path == "/api/accounts/refresh-points" and method == "POST":
                if not self._authorize():
                    return self._unauthorized()
                import redeem
                self._json(redeem.refresh_all_points())
            elif path == "/api/accounts/refresh-status" and method == "GET":
                if not self._authorize():
                    return self._unauthorized()
                import redeem
                self._json({"refreshing": redeem.is_refreshing_points()})
            elif path == "/api/accounts/start" and method == "POST":
                self._ep_accounts_start()
            elif path == "/api/accounts/stop" and method == "POST":
                self._ep_accounts_stop()
            elif path == "/api/accounts" and method == "PUT":
                self._ep_accounts_edit()
            elif path.startswith("/api/accounts/") and method == "DELETE":
                self._ep_accounts_delete(path[len("/api/accounts/"):])
            elif path == "/api/logs" and method == "GET":
                self._ep_logs_sse()
            elif path == "/api/overview" and method == "GET":
                self._ep_overview()
            elif path == "/api/system/env-check" and method == "GET":
                self._ep_env_check()
            elif path == "/api/system/env-install" and method == "POST":
                self._ep_env_install()
            elif path == "/api/jobs" and method == "GET":
                self._ep_jobs_list()
            elif path == "/api/jobs" and method == "POST":
                self._ep_jobs_add()
            elif path == "/api/jobs" and method == "PUT":
                self._ep_jobs_update()
            elif path.startswith("/api/jobs/") and method == "DELETE":
                self._ep_jobs_delete(path[len("/api/jobs/"):])
            elif path == "/api/jobs/run" and method == "POST":
                self._ep_jobs_run()
            elif path == "/api/jobs/stop" and method == "POST":
                self._ep_jobs_stop()
            elif path == "/api/jobs/history" and method == "GET":
                self._ep_jobs_history()
            elif path == "/api/jobs/cron-preview" and method == "POST":
                self._ep_jobs_cron_preview()
            elif path == "/api/tasks/summary" and method == "GET":
                self._ep_tasks_summary()
            elif path == "/api/tasks/run-missing" and method == "POST":
                self._ep_tasks_run_missing()
            elif path == "/api/platform/tasks" and method == "GET":
                if not self._authorize():
                    return self._unauthorized()
                self._ep_platform_tasks()
            elif path == "/api/platform/tasks/all" and method == "POST":
                if not self._authorize():
                    return self._unauthorized()
                self._ep_platform_tasks_all_start()
            elif path == "/api/platform/tasks/all" and method == "GET":
                if not self._authorize():
                    return self._unauthorized()
                self._ep_platform_tasks_all_status()
            elif path == "/api/update/check" and method == "GET":
                self._ep_update_check()
            elif path == "/api/update/run" and method == "POST":
                self._ep_update_run()
            elif path == "/api/redeem/config" and method == "GET":
                self._ep_redeem_config_get()
            elif path == "/api/redeem/config" and method == "PUT":
                self._ep_redeem_config_put()
            elif path == "/api/redeem/plan" and method == "GET":
                self._ep_redeem_plan()
            elif path == "/api/redeem/rewards" and method == "GET":
                self._ep_redeem_rewards()
            elif path == "/api/redeem/execute" and method == "POST":
                self._ep_redeem_execute()
            elif path == "/api/settings" and method == "GET":
                self._ep_settings_get()
            elif path == "/api/settings" and method == "PUT":
                self._ep_settings_put()
            elif path == "/api/feishu/test" and method == "POST":
                self._ep_feishu_test()
            elif path == "/api/feishu/chats" and method == "POST":
                self._ep_feishu_chats()
            else:
                self._json({"success": False, "msg": "接口不存在"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as ex:
            logs.fail("系统", "处理请求 %s %s 异常：%s" % (method, self.path, ex))
            try:
                self._json({"success": False, "msg": "服务器内部错误"}, status=500)
            except Exception:
                self.close_connection = True

    # ----- 静态文件 -----

    def _serve_static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WWWROOT, rel))
        if not full.startswith(os.path.normpath(WWWROOT)) or not os.path.isfile(full):
            self._json({"success": False, "msg": "Not Found"}, status=404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ================= 账号端点 =================

    def _ep_login(self):
        body = self._read_json()
        if body is None:
            body = {}
        saved = G.config.admin_password or "admin"
        if (body.get("password") or "") == saved:
            token, exp = AdminSessionStore.issue_with_expiry(G.config.session_token_hours)
            self._json({"success": True, "msg": "", "token": token, "expiresAt": exp})
        else:
            self._json({"success": False, "msg": "密码错误", "token": "", "expiresAt": 0})

    def _ep_logout(self):
        if not self._authorize():
            return self._unauthorized()
        AdminSessionStore.revoke(self._token)
        self._ok_web(success=True)

    def _ep_change_password(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        saved = G.config.admin_password or "admin"
        if (body.get("oldPassword") or "") != saved:
            return self._ok_web(success=False, msg="原密码错误")
        new_password = (body.get("newPassword") or "").strip()
        if not new_password:
            return self._ok_web(success=False, msg="新密码不能为空")
        G.config.admin_password = new_password
        ConfigStore.save(Paths.accounts_path, G.config.to_dict)
        AdminSessionStore.revoke_all()
        self._ok_web(success=True)

    def _ep_accounts_list(self):
        if not self._authorize():
            return self._unauthorized()
        now = int(time.time())
        out = []
        for account in G.config.accounts:
            key = keepalive.normalize_key(account.user)
            info = G.account_statuses.get(key)
            # 积分缓存（按原始手机号键）
            pc = G.points_cache.get(account.user) or {}
            points_fields = {
                "points": pc.get("points"),
                "pointsDetail": pc.get("detail") or {},
                "pointsCheckedAt": pc.get("checkedAt", 0),
                "pointsError": pc.get("error", ""),
            }
            if info is None:
                metrics = {k: ("" if k == "lastError" else 0) for k in [
                    "startedAt", "uptimeSeconds", "heartbeatSuccess", "heartbeatFailed",
                    "consecutiveFailures", "reconnectCount", "lastHeartbeatAt", "retryCount",
                    "nextRetryAt", "nextRestartAt"]}
                metrics["lastError"] = ""
                dto = {"name": account.name, "user": account.user, "key": key,
                       "isRunning": False, "statusText": "已停止", "desktops": [], "metrics": metrics}
                dto.update(points_fields)
            else:
                metrics = dict(info.metrics)
                if metrics.get("startedAt", 0) > 0:
                    metrics["uptimeSeconds"] = max(0, now - metrics["startedAt"])
                dto = {"name": account.name, "user": account.user, "key": key,
                       "isRunning": info.is_running, "statusText": info.status_text,
                       "desktops": info.desktops, "metrics": metrics}
                dto.update(points_fields)
            out.append(dto)

        # 等待验证码的待绑定账号（不含已配置的）
        # G._gate（RLock）保护 pending 字典读取：与写入端（添加/验证接口）互斥，
        # 锁内仅做字典读与列表填充，无耗时操作
        with G._gate:
            for pending_user in list(G.pending_logins.keys()):
                if any(a.get("user") == pending_user for a in out):
                    continue
                pending_cfg = G.pending_configs.get(pending_user)
                out.append({
                    "name": (pending_cfg.name if pending_cfg else "") or pending_user,
                    "user": pending_user,
                    "key": keepalive.normalize_key(pending_user),
                    "isRunning": False,
                    "statusText": "等待验证码",
                    "desktops": [],
                    "metrics": {k: (0 if k != "lastError" else "") for k in [
                        "startedAt", "uptimeSeconds", "heartbeatSuccess", "heartbeatFailed",
                        "consecutiveFailures", "reconnectCount", "lastHeartbeatAt", "retryCount",
                        "nextRetryAt", "nextRestartAt", "lastError"]},
                })
        self._json(out)

    def _ep_accounts_add(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        from store import AccountConfig
        account = AccountConfig.from_dict(body)
        if not account.user.strip() or not account.password.strip():
            return self._ok_web(status="Error", message="账号和密码不能为空")
        account.name = first_not_empty(account.name, account.user)
        account.device_code = resolve_device_code(account, Paths.data_dir)

        if keepalive.resolve_key(account.user, G.config.accounts) is not None:
            return self._ok_web(success=False, msg="该账号已存在，请勿重复添加")

        import ctyun_api
        api = ctyun_api.CtYunApi(account.device_code)
        logs.info(account.name, "尝试网页登录...")

        if not api.login(account.user, account.password):
            return self._ok_web(status="Error", message="登录失败，密码可能错误或验证码识别失败")

        if api.login_info.get("bondedDevice", False):
            upsert_account(account)
            ConfigStore.save(Paths.accounts_path, G.config.to_dict)
            keepalive.KeepAliveEngine.start(account)
            return self._ok_web(status="Success", message="登录成功并启动保活")

        logs.warn(account.name, "当前设备未绑定，发送验证码短信中...")
        if not api.get_sms_code(account.user):
            return self._ok_web(status="Error", message="发送短信验证码失败")

        G.pending_logins[account.user] = api
        G.pending_configs[account.user] = account

        status = G.statuses_get_or_add(keepalive.normalize_key(account.user))
        status.is_running = False
        status.status_text = "等待验证码"
        return self._ok_web(status="NeedSMS", message="短信验证码已发送")

    def _ep_accounts_verify(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        user = (body.get("user") or "").strip()
        code = (body.get("code") or "").strip()
        if not user or not code:
            return self._ok_web(status="Error", message="手机号和验证码不能为空")

        api = G.pending_logins.get(user)
        account = G.pending_configs.get(user)
        if api is None or account is None:
            return self._ok_web(status="Error", message="会话已超时，请重新添加账号进行登录")

        logs.info(account.name, "正在验证验证码: %s" % code)
        if not api.binding_device(code):
            return self._ok_web(status="Error", message="绑定失败，验证码可能错误或失效")

        upsert_account(account)
        ConfigStore.save(Paths.accounts_path, G.config.to_dict)
        G.pending_logins.pop(user, None)
        G.pending_configs.pop(user, None)

        keepalive.KeepAliveEngine.start(account)
        return self._ok_web(status="Success", message="绑定设备成功，保活任务已启动")

    def _ep_accounts_start(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        key = keepalive.resolve_key(body.get("name") or "", G.config.accounts)
        if key is None:
            return self._ok_web(success=False, msg="未找到配置账号")
        account = keepalive.find_account(key, G.config.accounts)
        logs.info(account.name, "手动启动保活中...")
        if keepalive.KeepAliveEngine.start(account):
            return self._ok_web(success=True)
        return self._ok_web(success=False, msg="启动失败")

    def _ep_accounts_stop(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        key = keepalive.resolve_key(body.get("name") or "", G.config.accounts)
        if key is None:
            return self._ok_web(success=False, msg="未找到配置账号")
        success = keepalive.KeepAliveEngine.stop(key)
        if success:
            return self._ok_web(success=True)
        return self._ok_web(success=False, msg="账号保活已处于停止状态")

    def _ep_accounts_edit(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        key = keepalive.resolve_key(body.get("key") or "", G.config.accounts)
        if key is None:
            return self._ok_web(success=False, msg="未找到配置账号")
        account = keepalive.find_account(key, G.config.accounts)
        changed = False
        name = body.get("name") or ""
        password = body.get("password") or ""
        if name:
            account.name = name.strip()
            changed = True
        if password:
            account.password = password.strip()
            changed = True
        if changed:
            ConfigStore.save(Paths.accounts_path, G.config.to_dict)
        return self._ok_web(success=True, msg="修改已保存；修改密码需重启保活后生效")

    def _ep_accounts_delete(self, key):
        if not self._authorize():
            return self._unauthorized()
        resolved = keepalive.resolve_key(key, G.config.accounts)
        if resolved is None:
            return self._ok_web(success=False, msg="未找到配置账号")
        keepalive.KeepAliveEngine.stop(resolved)
        account = keepalive.find_account(resolved, G.config.accounts)
        if account is not None:
            G.config.accounts.remove(account)
            ConfigStore.save(Paths.accounts_path, G.config.to_dict)
            G.statuses_pop(resolved)
        return self._ok_web(success=True)

    # ================= 系统端点 =================

    def _ep_logs_sse(self):
        if not self._authorize():
            # C# 直接 401 无 body
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        sid, sub, history = logs.Log.subscribe()
        try:
            for entry in history:
                self.wfile.write(("data: " + json.dumps(entry, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    entry = sub.q.get(timeout=5)
                    self.wfile.write(("data: " + json.dumps(entry, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # 注释行保活，兼做断连检测
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass  # SSE 连接断开，正常收尾
        finally:
            logs.Log.unsubscribe(sid)

    def _ep_overview(self):
        if not self._authorize():
            return self._unauthorized()
        cfg = G.config
        now = int(time.time())
        statuses = list(G.account_statuses.values())
        running = sum(1 for s in statuses if s.is_running)
        uptime = sum(max(0, now - s.metrics["startedAt"])
                     for s in statuses if s.metrics.get("startedAt", 0) > 0)

        today_s, today_f = _job_service().today_stats()
        next_at, next_name = _job_service().next_job()

        # 保活成功率：全部账号心跳 成功/(成功+失败)，无数据时 -1
        hb_ok = sum(int((s.metrics.get("heartbeatSuccess", 0) or 0)) for s in statuses)
        hb_bad = sum(int((s.metrics.get("heartbeatFailed", 0) or 0)) for s in statuses)
        if hb_ok + hb_bad > 0:
            success_rate = round(hb_ok * 100.0 / (hb_ok + hb_bad), 1)
        else:
            success_rate = -1

        # 总览积分余额：所有账号缓存积分之和（从未查询过时 -1 表示不可用）
        total_points = -1
        if cfg.accounts:
            _sum = 0
            _any = False
            for _a in cfg.accounts:
                pc = G.points_cache.get(_a.user) or {}
                if pc.get("points") is not None:
                    _sum += pc["points"]
                    _any = True
            if _any:
                total_points = _sum

        self._json({
            "accountTotal": len(cfg.accounts),
            "accountRunning": running,
            "keepAliveUptimeSeconds": uptime,
            "keepAliveSuccessRate": success_rate,  # 心跳成功率 %（-1=暂无数据）
            "todayJobSuccess": today_s,
            "todayJobFailed": today_f,
            "nextJobAt": next_at,
            "nextJobName": next_name,
            "pointsBalance": total_points,  # 全部账号通用积分之和（缓存，点「刷新积分」更新）
            "envOk": bool(G.last_env_check and G.last_env_check.get("allOk", False)),
            "schedulerRunning": _job_service().is_running,
        })

    def _ep_env_check(self):
        if not self._authorize():
            return self._unauthorized()
        result = envprobe.probe()
        result["installing"] = envprobe.is_installing()
        G.last_env_check = result
        self._json(result)

    def _ep_env_install(self):
        if not self._authorize():
            return self._unauthorized()
        self._json(envprobe.install_missing())

    # ================= 自动更新 =================

    def _ep_update_check(self):
        if not self._authorize():
            return self._unauthorized()
        try:
            import updater
            self._json(updater.check_update())
        except Exception as ex:
            self._json({"ok": False, "hasUpdate": False, "error": str(ex)})

    def _ep_update_run(self):
        if not self._authorize():
            return self._unauthorized()
        try:
            import updater
            result = updater.perform_update()
        except Exception as ex:
            self._json({"ok": False, "updated": False, "error": str(ex)})
            return
        if result.get("updated"):
            logs.ok("系统", "代码已更新（%d 个文件），即将自动重启服务" % len(result.get("changed", [])))
        # 先回包，再由后台线程触发重启，避免前端收不到响应
        self._json(result)
        if result.get("updated"):
            threading.Thread(target=_restart_after_update, name="update-restart",
                             daemon=True).start()

    def _ep_tasks_summary(self):
        if not self._authorize():
            return self._unauthorized()
        summary = _job_service().task_summary()
        # 合并「最近一次全量平台任务快照」的达标信息：
        # platformDone=true/false 表示该账号的平台任务今日已达标/未达标；
        # null 表示未知（未拉取过快照，或该账号没有对应平台任务）。
        lookup = _platform_done_lookup()
        for acc in summary.get("accounts", []):
            per = lookup.get(acc.get("accountUser") or "")
            for t in acc.get("tasks", []):
                t["platformDone"] = None if per is None else per.get(t.get("jobType"))
        with _platform_all_lock:
            summary["platformSnapshotAt"] = _platform_all_state.get("finishedAt") or 0
        self._json(summary)

    def _ep_tasks_run_missing(self):
        if not self._authorize():
            return self._unauthorized()
        self._json(_job_service().run_missing_ai_chat())

    def _ep_platform_tasks(self):
        """实时拉取指定账号的全部平台任务完成情况。

        GET /api/platform/tasks?user=<账号手机号或备注名>
        返回 {"success", "msg", "user", "tasks": [{name, progress, limit, done, reward}]}
        done 三态：true=已完成 / false=未完成 / null=无法判断。
        """
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        user = (query.get("user", [""])[0] or "").strip()
        if not user:
            return self._json({"success": False, "msg": "缺少 user 参数", "user": "", "tasks": []})
        import keepalive as _ka
        acc = _ka.find_account(user, G.config.accounts)
        if acc is None:
            return self._json({"success": False, "msg": "账号不存在", "user": user, "tasks": []})
        try:
            import ctyun_api
            import redeem
            api = ctyun_api.CtYunApi(acc.device_code)
            if not api.login(acc.user, acc.password):
                return self._json({"success": False, "msg": "平台登录失败", "user": user, "tasks": []})
            tasks, err = redeem.task_overview(api)
            if err:
                return self._json({"success": False, "msg": err, "user": user, "tasks": []})
            done_n = sum(1 for t in tasks if t["done"] is True)
            # 缓存本次结果：任务汇总据此显示「已完成（平台达标）」
            with _platform_user_lock:
                _platform_user_cache[acc.user] = {"tasks": tasks, "at": time.time()}
            logs.info("任务", "[平台任务] %s 实时查询：%d 项任务，已完成 %d"
                      % (acc.user, len(tasks), done_n))
            self._json({"success": True, "msg": "", "user": user, "tasks": tasks})
        except Exception as ex:
            self._json({"success": False, "msg": str(ex), "user": user, "tasks": []})

    # ---------- 全部账号平台任务统计（后台任务 + 状态轮询） ----------

    def _ep_platform_tasks_all_start(self):
        """启动全量统计：逐个账号登录平台拉取任务列表（串行，避免验证码并发）。"""
        with _platform_all_lock:
            if _platform_all_state["running"]:
                return self._json({"started": False, "running": True, "msg": "统计进行中"})
            _platform_all_state.update({
                "running": True, "startedAt": time.time(), "finishedAt": 0,
                "accounts": [], "total": {"done": 0, "doing": 0, "todo": 0},
                "ok": False, "msg": "",
            })
        threading.Thread(target=_platform_all_worker,
                         name="platform-tasks-all", daemon=True).start()
        self._json({"started": True, "running": True, "msg": ""})

    def _ep_platform_tasks_all_status(self):
        with _platform_all_lock:
            snap = {
                "running": _platform_all_state["running"],
                "startedAt": _platform_all_state["startedAt"],
                "finishedAt": _platform_all_state["finishedAt"],
                "ok": _platform_all_state["ok"],
                "msg": _platform_all_state["msg"],
                "accounts": _platform_all_state["accounts"],
                "total": dict(_platform_all_state["total"]),
            }
        self._json(snap)

    # ================= 任务端点 =================

    def _validate_job(self, body):
        """任务新增/更新统一校验。返回 (job 或 None, errmsg)。"""
        import cronx
        from jobs import ScheduledJob, job_type_valid
        if not body:
            return None, "请求体为空"
        job = ScheduledJob.from_dict(body)
        if not job.name.strip():
            return None, "任务名称不能为空"
        if not job_type_valid(job.type):
            return None, "任务类型非法（应为 ai_chat 或 pc_hang）"
        try:
            cronx.CronExpression.try_parse(job.cron)
        except cronx.CronError as ex:
            return None, "Cron 表达式非法：" + str(ex)
        if keepalive.resolve_key(job.account_user, G.config.accounts) is None:
            return None, "关联账号不存在"
        if job.timeout_minutes < 1 or job.timeout_minutes > 1440:
            return None, "超时时间必须在 1-1440 分钟之间"
        return job, ""

    def _ep_jobs_list(self):
        if not self._authorize():
            return self._unauthorized()
        self._json(_job_service().snapshot())

    def _ep_jobs_add(self):
        if not self._authorize():
            return self._unauthorized()
        job, err = self._validate_job(self._read_json())
        if job is None:
            return self._ok_web(success=False, msg=err)
        if not job.id:
            job.id = __import__("uuid").uuid4().hex
        _job_service().upsert(job)
        return self._ok_web(success=True, msg=job.id)

    def _ep_jobs_update(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        job, err = self._validate_job(body)
        if job is None:
            return self._ok_web(success=False, msg=err)
        if not job.id:
            return self._ok_web(success=False, msg="任务 Id 不能为空")
        _job_service().upsert(job)
        return self._ok_web(success=True, msg=job.id)

    def _ep_jobs_delete(self, job_id):
        if not self._authorize():
            return self._unauthorized()
        _job_service().remove(job_id)
        return self._ok_web(success=True)

    def _ep_jobs_run(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        job = _job_service().find(body.get("id") or "")
        if job is None:
            return self._ok_web(success=False, msg="任务不存在")
        if job.running:
            return self._ok_web(success=False, msg="任务正在运行中")

        # 环境自检
        env = envprobe.probe()
        if not env["allOk"]:
            bad = [it["displayName"] for it in env["items"]
                   if it["name"] != "chromium" and not it["ok"]]
            fixes = [it["fixCommand"] for it in env["items"]
                     if it["name"] != "chromium" and not it["ok"]]
            msg = "环境不可用：" + "、".join(bad) + "。修复命令：" + "; ".join(fixes)
            return self._ok_web(success=False, msg=msg)

        # 浏览器互斥（非获取式探测）
        if mutex.BrowserMutex.is_held(job.type):
            return self._ok_web(success=False, msg=mutex.mutex_message())

        # 异步执行，立即返回（C7）
        threading.Thread(target=_job_service().execute, args=(job, "manual"),
                         name="job-" + job.id, daemon=True).start()
        return self._json({"success": True, "msg": "", "runId": job.id})

    def _ep_jobs_stop(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        job = _job_service().find(body.get("id") or "")
        if job is None:
            return self._ok_web(success=False, msg="任务不存在")
        # 真正终止脚本进程并释放浏览器互斥，避免停止后其它任务被永久挡住
        # 进程登记键为「账号:任务类型」组合键，与 scriptrunner.run 侧保持一致
        killed = False
        try:
            import scriptrunner
            killed = scriptrunner.kill_running(
                scriptrunner.make_run_key(job.account_user or "", job.type))
        except Exception:
            killed = False
        try:
            import mutex
            # force=True：用户主动停止，无论锁当前登记持有者是谁都强制释放
            mutex.BrowserMutex.release(job.type, force=True)
        except Exception:
            pass
        job.running = False
        logs.info("任务", "[%s] 已手动停止（终止进程：%s，浏览器互斥已释放）"
                  % (job.name, "是" if killed else "无可终止进程"))
        return self._ok_web(success=True)

    def _ep_jobs_history(self):
        if not self._authorize():
            return self._unauthorized()
        self._json(_job_service().history_snapshot())

    def _ep_jobs_cron_preview(self):
        if not self._authorize():
            return self._unauthorized()
        import cronx
        body = self._read_json() or {}
        try:
            cron = cronx.CronExpression.try_parse(body.get("cron") or "")
        except cronx.CronError as ex:
            return self._json({"valid": False, "error": str(ex), "description": "", "nextTimes": []})

        times = []
        from_dt = datetime_now()
        for _ in range(3):
            nxt = cron.get_next_occurrence(from_dt, from_dt + _timedelta_days(366))
            if nxt is None:
                break
            times.append(nxt.strftime("%Y-%m-%d %H:%M"))
            from_dt = nxt + _timedelta_minutes(1)
        self._json({"valid": True, "error": "", "description": cron.describe(), "nextTimes": times})

    # ================= 兑换端点 =================

    def _ep_redeem_config_get(self):
        if not self._authorize():
            return self._unauthorized()
        cfg = redeem.load_redeem_config()
        self._json(cfg.to_dict())

    def _ep_redeem_config_put(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        cfg = redeem.RedeemConfig.from_dict(body)
        redeem.save_redeem_config(cfg)
        return self._ok_web(success=True, msg="已写入 " + Paths.redeem_config_path)

    def _ep_redeem_plan(self):
        if not self._authorize():
            return self._unauthorized()
        cfg = redeem.load_redeem_config()
        should, reason = redeem.evaluate_policy(cfg, _date_today())
        self._json({
            "shouldRedeem": should,
            "reason": reason,
            "channelAState": G.channel_a_state,
            "configPath": Paths.redeem_config_path,
            "points": -1,
            "lastRedeemDate": cfg.last_redeem_date,
        })

    def _ep_redeem_rewards(self):
        if not self._authorize():
            return self._unauthorized()
        api = redeem._login_first()
        if api is None:
            return self._ok_web(success=False, msg="无可用账号或登录失败")
        items = redeem.get_rewards(api)
        if items is None:
            return self._ok_web(success=False,
                                msg="selforder 接口登录态失效（code=40010）或拉取失败，请走 Python 通道 B")
        self._json(items)

    def _ep_redeem_execute(self):
        if not self._authorize():
            return self._unauthorized()
        cfg = redeem.load_redeem_config()
        ok, msg = redeem.execute(cfg, manual=True)  # 前端手动触发：跳过兑换日限制
        return self._ok_web(success=ok, msg=msg)

    # ================= 设置端点 =================

    def _ep_settings_get(self):
        if not self._authorize():
            return self._unauthorized()
        cfg = G.config
        self._json({
            "keepAliveSeconds": cfg.keep_alive_seconds,
            "sessionRestartMinutes": cfg.session_restart_minutes,
            "sessionTokenHours": cfg.session_token_hours,
            "pythonExecutable": cfg.python_executable,
            "scriptsDir": cfg.scripts_dir,
            "aiChatTimeoutMinutes": cfg.ai_chat_timeout_minutes,
            "pcHangTimeoutMinutes": cfg.pc_hang_timeout_minutes,
            "pcHangSeconds": cfg.pc_hang_seconds,
            "bootWaitRounds": cfg.boot_wait_rounds,
            "bootWaitSecondsPerRound": cfg.boot_wait_seconds_per_round,
            "browserMutexMode": cfg.browser_mutex_mode,
            "pollIntervalSeconds": cfg.poll_interval_seconds,
            "feishuWebhook": getattr(cfg, "feishu_webhook", ""),
            "feishuSecret": getattr(cfg, "feishu_secret", ""),
            "feishuAppId": getattr(cfg, "feishu_app_id", ""),
            "feishuAppSecret": getattr(cfg, "feishu_app_secret", ""),
            "feishuChatId": getattr(cfg, "feishu_chat_id", ""),
            # 只读诊断信息
            "dataDir": Paths.data_dir,
            "accountsPath": Paths.accounts_path,
            "redeemConfigPath": Paths.redeem_config_path,
            "jobsPath": Paths.jobs_path,
            "scriptsResolvedDir": Paths.scripts_dir,
            "isContainer": is_container(),
        })

    def _ep_settings_put(self):
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        cfg = G.config

        def geti(key, default):
            try:
                return int(body.get(key, default))
            except (TypeError, ValueError):
                return default

        cfg.keep_alive_seconds = geti("keepAliveSeconds", 60)
        cfg.session_restart_minutes = geti("sessionRestartMinutes", 1440)
        cfg.session_token_hours = geti("sessionTokenHours", 12)
        cfg.python_executable = str(body.get("pythonExecutable", "") or "")
        cfg.ai_chat_timeout_minutes = geti("aiChatTimeoutMinutes", 15)
        cfg.pc_hang_timeout_minutes = geti("pcHangTimeoutMinutes", 100)
        cfg.pc_hang_seconds = geti("pcHangSeconds", 4800)
        cfg.boot_wait_rounds = geti("bootWaitRounds", 3)
        cfg.boot_wait_seconds_per_round = geti("bootWaitSecondsPerRound", 60)
        cfg.browser_mutex_mode = str(body.get("browserMutexMode", "Global") or "Global")
        cfg.poll_interval_seconds = geti("pollIntervalSeconds", 5)
        cfg.feishu_webhook = str(body.get("feishuWebhook", "") or "").strip()
        cfg.feishu_secret = str(body.get("feishuSecret", "") or "").strip()
        cfg.feishu_app_id = str(body.get("feishuAppId", "") or "").strip()
        cfg.feishu_app_secret = str(body.get("feishuAppSecret", "") or "").strip()
        cfg.feishu_chat_id = str(body.get("feishuChatId", "") or "").strip()

        new_scripts_dir = str(body.get("scriptsDir", "") or "")
        if new_scripts_dir != cfg.scripts_dir:
            cfg.scripts_dir = new_scripts_dir
            Paths.refresh_scripts_dir(cfg.scripts_dir)

        ConfigStore.save(Paths.accounts_path, G.config.to_dict)
        return self._ok_web(success=True)

    def _ep_feishu_test(self):
        """发送飞书测试消息。body 可带全部飞书字段（未保存前先测试）。
        应用模式未填 chat_id 时自动取第一个群，响应带 usedChatId 供前端回填。"""
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        import feishu
        cfg = {
            "webhook": str(body.get("feishuWebhook", "") or "").strip(),
            "secret": str(body.get("feishuSecret", "") or "").strip(),
            "app_id": str(body.get("feishuAppId", "") or "").strip(),
            "app_secret": str(body.get("feishuAppSecret", "") or "").strip(),
            "chat_id": str(body.get("feishuChatId", "") or "").strip(),
        }
        ok, msg, used_chat_id = feishu.send_test(cfg)
        self._json({"success": ok, "msg": msg, "usedChatId": used_chat_id})

    def _ep_feishu_chats(self):
        """探测应用机器人所在的群聊列表（应用模式）。"""
        if not self._authorize():
            return self._unauthorized()
        body = self._read_json() or {}
        import feishu
        app_id = str(body.get("feishuAppId", "") or "").strip()
        app_secret = str(body.get("feishuAppSecret", "") or "").strip()
        if not app_id or not app_secret:
            cfg = feishu._get_config()
            app_id = app_id or cfg.get("app_id", "")
            app_secret = app_secret or cfg.get("app_secret", "")
        if not app_id or not app_secret:
            self._json({"success": False, "msg": "未配置 App ID / App Secret"})
            return
        items, err = feishu.list_chats(app_id, app_secret)
        if err:
            self._json({"success": False, "msg": err})
            return
        self._json({"success": True, "chats": items})


# ---------- 小工具 ----------

def _job_service():
    import jobs
    return jobs.JobService


def datetime_now():
    from datetime import datetime
    return datetime.now()


def _timedelta_days(n):
    from datetime import timedelta
    return timedelta(days=n)


def _timedelta_minutes(n):
    from datetime import timedelta
    return timedelta(minutes=n)


def _date_today():
    from datetime import date
    return date.today()
