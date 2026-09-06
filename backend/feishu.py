# -*- coding: utf-8 -*-
"""
飞书错误推送。支持两种模式（二选一，都配置时优先 Webhook）：
  1. Webhook 模式：群自定义机器人 Webhook 地址（可选加签）。
  2. 应用模式：飞书开放平台自建应用 App ID + App Secret，
     自动换取 tenant_access_token，自动探测机器人所在群聊并发送。

设计要点：
- 非阻塞：notify_error() 只入队，后台守护线程发送，绝不拖慢主流程。
- 防轰炸：同源同摘要 5 分钟去重；每分钟最多 8 条。
- 熔断：连续 3 次发送失败后静默 10 分钟，期间丢弃推送并记本地日志。
- 零依赖：仅标准库 urllib/hmac/hashlib/base64/json。
"""
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request

import logs

_WEBHOOK_TIMEOUT = 10
_DEDUP_SECONDS = 300          # 同 (source, 摘要) 去重窗口
_RATE_MAX_PER_MIN = 8         # 每分钟最多推送条数
_CIRCUIT_THRESHOLD = 3        # 连续失败 N 次后熔断
_CIRCUIT_COOLDOWN = 600       # 熔断静默秒数
_OPEN_BASE = "https://open.feishu.cn/open-apis"

_gate = threading.Lock()
_recent = {}                  # (source, 摘要) -> 上次推送 ts
_rate_stamps = []             # 最近 60s 推送时间戳列表
_fail_streak = 0              # 连续失败计数
_muted_until = 0.0            # 熔断截止时刻
_queue = []                   # 待推送 (webhook, secret, text) 或 ("app", text)
_worker_started = False
_token_cache = {"token": "", "expire_at": 0.0}   # 应用模式凭证缓存


def _get_config():
    """读取配置（晚导入避免循环依赖）。返回 dict：webhook/secret/app_id/app_secret/chat_id。"""
    try:
        import store
        cfg = store.G.config
        if not cfg:
            return {}
        return {
            "webhook": (getattr(cfg, "feishu_webhook", "") or "").strip(),
            "secret": (getattr(cfg, "feishu_secret", "") or "").strip(),
            "app_id": (getattr(cfg, "feishu_app_id", "") or "").strip(),
            "app_secret": (getattr(cfg, "feishu_app_secret", "") or "").strip(),
            "chat_id": (getattr(cfg, "feishu_chat_id", "") or "").strip(),
        }
    except Exception:
        return {}


# ---------- Webhook 模式 ----------

def _sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人「加签」算法。"""
    string_to_sign = "%d\n%s" % (timestamp, secret)
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _post_webhook(webhook: str, secret: str, text: str):
    timestamp = int(time.time())
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _sign(secret, timestamp)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
        obj = json.loads(body) if body else {}
        code = obj.get("code", obj.get("StatusCode", 0))
        if code == 0:
            return True, "ok"
        return False, "飞书返回 code=%s, msg=%s" % (code, obj.get("msg", ""))
    except Exception as ex:
        return False, str(ex)


# ---------- 应用模式（App ID + App Secret） ----------

def _http_json(method: str, url: str, payload=None, token: str = ""):
    """通用请求。返回 (ok, code, msg, data)。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
        obj = json.loads(body) if body else {}
        code = obj.get("code", 0)
        if code == 0:
            return True, 0, "ok", obj.get("data") or {}
        return False, code, obj.get("msg") or ("code=%s" % code), obj.get("data") or {}
    except Exception as ex:
        return False, -1, str(ex), {}


def _tenant_token(app_id: str, app_secret: str, force_refresh: bool = False):
    """获取 tenant_access_token（带缓存，提前 5 分钟过期）。返回 (token, err)。

    注意：该接口返回结构与常规开放平台接口不同——token/expire 在 JSON 顶层，
    不嵌套在 data 里，因此这里独立解析，不走 _http_json。
    """
    with _gate:
        if (not force_refresh and _token_cache["token"]
                and time.time() < _token_cache["expire_at"]):
            return _token_cache["token"], ""
    payload = {"app_id": app_id, "app_secret": app_secret}
    req = urllib.request.Request(
        _OPEN_BASE + "/auth/v3/tenant_access_token/internal",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
        obj = json.loads(body) if body else {}
    except Exception as ex:
        return "", "获取 tenant_access_token 请求异常：" + str(ex)
    code = obj.get("code", 0)
    if code != 0:
        return "", "获取 tenant_access_token 失败：code=%s, msg=%s（请核对 App ID / App Secret）" % (
            code, obj.get("msg", ""))
    token = obj.get("tenant_access_token") or ""
    expire = int(obj.get("expire", 7200) or 7200)
    if not token:
        return "", "飞书未返回 tenant_access_token：响应=%s" % json.dumps(obj, ensure_ascii=False)[:200]
    with _gate:
        _token_cache["token"] = token
        _token_cache["expire_at"] = time.time() + max(60, expire - 300)
    return token, ""


def list_chats(app_id: str, app_secret: str):
    """列出机器人所在的群聊。返回 (items, err)；items=[{chat_id, name}]。"""
    token, err = _tenant_token(app_id, app_secret, force_refresh=True)
    if err:
        return None, err
    ok, code, msg, data = _http_json(
        "GET", _OPEN_BASE + "/im/v1/chats?page_size=50", token=token)
    if not ok:
        if code in (99991663, 99991661, 99991668):
            # 凭证失效重试一次
            token, err = _tenant_token(app_id, app_secret, force_refresh=True)
            if err:
                return None, err
            ok, code, msg, data = _http_json(
                "GET", _OPEN_BASE + "/im/v1/chats?page_size=50", token=token)
            if not ok:
                return None, "获取群列表失败：%s" % msg
        else:
            return None, "获取群列表失败：%s（应用需开通 im:chat 只读权限）" % msg
    items = [{"chat_id": c.get("chat_id", ""),
              "name": c.get("name") or c.get("chat_id", "")}
             for c in (data.get("items") or []) if c.get("chat_id")]
    return items, ""


def _send_via_app(cfg: dict, text: str):
    """应用模式发送。chat_id 为空时自动取机器人所在第一个群。返回 (ok, msg, chat_id)。"""
    app_id, app_secret = cfg.get("app_id", ""), cfg.get("app_secret", "")
    if not app_id or not app_secret:
        return False, "未配置 App ID / App Secret", ""
    token, err = _tenant_token(app_id, app_secret)
    if err:
        return False, err, ""

    chat_id = cfg.get("chat_id", "")
    used = chat_id
    if not chat_id:
        items, err = list_chats(app_id, app_secret)
        if err:
            return False, err, ""
        if not items:
            return False, "机器人未加入任何群聊（请先把应用机器人拉进目标群）", ""
        used = items[0]["chat_id"]

    body = {
        "receive_id": used,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    ok, code, msg, _ = _http_json(
        "POST", _OPEN_BASE + "/im/v1/messages?receive_id_type=chat_id",
        payload=body, token=token)
    if not ok:
        return False, "发送消息失败：%s" % msg, used
    return True, "ok", used


def _send(cfg: dict, text: str):
    """按配置选择通道发送。返回 (ok, msg, used_chat_id)。"""
    if cfg.get("webhook"):
        ok, msg = _post_webhook(cfg["webhook"], cfg.get("secret", ""), text)
        return ok, msg, ""
    if cfg.get("app_id") and cfg.get("app_secret"):
        return _send_via_app(cfg, text)
    return False, "未配置飞书 Webhook 或 App ID/Secret", ""


def _summary(message: str) -> str:
    """去重摘要：取首行前 80 字符。"""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return first[:80]


def notify_error(source: str, message: str):
    """错误日志出口挂钩：条件过滤后异步推送到飞书。永不抛异常。"""
    try:
        cfg = _get_config()
        if not cfg.get("webhook") and not (cfg.get("app_id") and cfg.get("app_secret")):
            return  # 未配置 → 静默关闭
        now = time.time()
        key = (source, _summary(message))
        global _fail_streak, _muted_until
        with _gate:
            if now < _muted_until:
                return  # 熔断中
            if _recent.get(key) and now - _recent[key] < _DEDUP_SECONDS:
                return  # 去重窗口内重复错误
            _recent[key] = now
            if len(_recent) > 256:
                for k in [k for k, v in _recent.items() if now - v > _DEDUP_SECONDS]:
                    _recent.pop(k, None)
            while _rate_stamps and now - _rate_stamps[0] > 60:
                _rate_stamps.pop(0)
            if len(_rate_stamps) >= _RATE_MAX_PER_MIN:
                return  # 超出频率上限
            _rate_stamps.append(now)
            text = "【CtYun 错误告警】[%s]\n%s\n时间：%s" % (
                source, (message or "").strip()[:800],
                time.strftime("%Y-%m-%d %H:%M:%S"))
            _queue.append((dict(cfg), text))
            _ensure_worker_locked()
    except Exception:
        pass


def _ensure_worker_locked():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    t = threading.Thread(target=_worker, name="feishu-push", daemon=True)
    t.start()


def _worker():
    """后台发送循环：逐条发送队列中的消息。"""
    global _fail_streak, _muted_until
    while True:
        item = None
        with _gate:
            if _queue:
                item = _queue.pop(0)
        if item is None:
            time.sleep(1)
            continue
        cfg, text = item
        ok, msg, _ = _send(cfg, text)
        with _gate:
            if ok:
                _fail_streak = 0
            else:
                _fail_streak += 1
                if _fail_streak >= _CIRCUIT_THRESHOLD:
                    _muted_until = time.time() + _CIRCUIT_COOLDOWN
                    _fail_streak = 0
        if not ok:
            logs._write_line("飞书", "推送失败（不影响系统运行）：" + msg)


def send_test(cfg: dict = None):
    """发送测试消息（供设置页「测试」按钮）。返回 (ok, msg, used_chat_id)。同步执行。"""
    cfg = cfg or {}
    if not cfg.get("webhook") and not (cfg.get("app_id") and cfg.get("app_secret")):
        # 表单为空时回落到已保存配置
        cfg = _get_config()
    if not cfg.get("webhook") and not (cfg.get("app_id") and cfg.get("app_secret")):
        return False, "未配置飞书 Webhook 或 App ID/App Secret", ""
    text = ("【CtYun 测试消息】\n飞书推送配置成功 ✓\n时间：%s\n\n"
            "之后系统错误日志（任务异常、兑换失败等）会自动推送到这里。" %
            time.strftime("%Y-%m-%d %H:%M:%S"))
    return _send(cfg, text)
