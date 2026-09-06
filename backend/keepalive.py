# -*- coding: utf-8 -*-
"""
保活内核（对应 C# KeepAliveEngine.cs + KeepAliveRestarter.cs）。

会话状态机（每账号一条外层循环）：
  登录 → 拉设备列表 → 开机等待 → 逐个 connect → 每台设备一条 WebSocket worker
  → 周期 keepAliveSeconds 强制重连 / 24h 会话重建 → 外层循环重建会话
  → 任一环节失败抛 SessionFailed → 退避 {30,60,120,300,600}s 重试。
"""
import base64
import json
import os
import threading
import time
from datetime import datetime, timedelta

import ctyun_api
import logs
import store
from store import G
from wire import Encryption, SendInfo
from wsclient import WSClient, WSClosed, WSTimeout


class SessionFailed(Exception):
    """会话级失败：由外层循环驱动退避重试。"""


def backoff_seconds(fail_count: int) -> int:
    ladder = [30, 60, 120, 300, 600]
    idx = max(0, fail_count)
    if idx >= len(ladder):
        idx = len(ladder) - 1
    return ladder[idx]


class KeepAliveSession:
    """单个账号的保活会话句柄。"""

    def __init__(self, key, display_name, user, account):
        self.key = key
        self.display_name = display_name or user
        self.user = user
        self.account = account          # AccountConfig 引用
        self.api = ctyun_api.CtYunApi(account.device_code)
        self.stop = threading.Event()   # 会话停止信号
        self.thread = None
        self.exited = threading.Event()


def _cancelled(session) -> bool:
    return session.stop.is_set() or G.global_stop.is_set()


def _interruptible_wait(session, seconds) -> bool:
    """等待 seconds 秒；返回 True 表示已被取消。"""
    end = time.monotonic() + seconds
    while not _cancelled(session):
        remain = end - time.monotonic()
        if remain <= 0:
            return False
        if session.stop.wait(min(0.5, remain)) or G.global_stop.wait(0):
            if _cancelled(session):
                return True
    return True


def _touch_metrics(key, fn):
    status = G.account_statuses.get(key)
    if status is not None:
        fn(status.metrics)


def _update_status(key, fn):
    status = G.account_statuses.get(key)
    if status is not None:
        fn(status)


def _update_desktop_status(key, desktop_code, text):
    status = G.account_statuses.get(key)
    if status is None:
        return
    for d in status.desktops:
        if d.get("code") == desktop_code:
            d["status"] = text


class KeepAliveEngine:
    @staticmethod
    def start(account) -> bool:
        """启动保活：先停旧会话再建新会话（对应 StartAsync）。"""
        key = normalize_key(account.user)
        KeepAliveEngine.stop(key)

        session = KeepAliveSession(key, account.name or account.user, account.user, account)
        G.active_workers[key] = session
        G.statuses_get_or_add(key)

        session.thread = threading.Thread(
            target=KeepAliveEngine._run_account_loop, args=(account, session),
            name="keepalive-" + key, daemon=True)
        session.thread.start()
        return True

    @staticmethod
    def stop(key) -> bool:
        """停止会话：移除 → 置停止 → 等待退出（最多 15s）。未找到返回 False。"""
        session = G.active_workers.pop(key, None)
        if session is None:
            return False
        session.stop.set()
        if session.thread is not None:
            session.thread.join(timeout=15)
        return True

    # ---------- 外层会话循环 ----------

    @staticmethod
    def _run_account_loop(account, session):
        key = session.key
        cfg = G.config
        fail = 0
        try:
            while not _cancelled(session):
                session_start = time.time()
                try:
                    KeepAliveEngine._run_session(account, session)
                    # 正常返回 = 需要重建会话（24h 重启或连接全部结束），健康则清零退避
                    if time.time() - session_start >= cfg.min_healthy_session_seconds:
                        fail = 0
                except SessionFailed as ex:
                    fail += 1
                    delay = backoff_seconds(fail)
                    now = int(time.time())

                    def touch(m, fail=fail, delay=delay, msg=str(ex), now=now):
                        m["consecutiveFailures"] = fail
                        m["retryCount"] = fail
                        m["nextRetryAt"] = now + delay
                        m["lastError"] = msg

                    _touch_metrics(key, touch)

                    def set_status(s):
                        s.is_running = False
                        s.status_text = "重试中"

                    _update_status(key, set_status)
                    logs.warn(session.display_name,
                              "会话失败，%d 秒后第 %d 次重试：%s" % (delay, fail, ex))
                    if _interruptible_wait(session, delay):
                        break
                except Exception as ex:  # 兜底：任何意外都不让线程死亡
                    logs.fail(session.display_name, "保活循环异常：" + str(ex))
                    if _interruptible_wait(session, 30):
                        break
        finally:

            def fin_status(s):
                s.is_running = False
                s.status_text = "已停止"

            _update_status(key, fin_status)

            def fin_metrics(m):
                m["nextRetryAt"] = 0
                m["nextRestartAt"] = 0

            _touch_metrics(key, fin_metrics)
            session.exited.set()

    # ---------- 单趟会话 ----------

    @staticmethod
    def _run_session(account, session):
        key = session.key
        cfg = G.config
        api = session.api
        now = int(time.time())
        _touch_metrics(key, lambda m: m.__setitem__("startedAt", now))

        def st0(s):
            s.is_running = True
            s.status_text = "登录中"
            s.desktops = []

        _update_status(key, st0)

        if not api.login(account.user, account.password):
            raise SessionFailed("登录失败或设备未绑定")
        if not api.login_info.get("bondedDevice", False):
            raise SessionFailed("登录失败或设备未绑定")

        desktop_list = api.get_client_list()
        if not desktop_list:
            raise SessionFailed("未获取到可用云电脑/云手机")

        # 开机等待：任一设备非"运行中"时主动发送开机指令并轮询，超时抛失败（KA-03）
        # ★ 此前只等待不开机：云电脑被关机后永远等不到"运行中"，保活必然失败停机
        if any(d.get("useStatusText") != "运行中" for d in desktop_list):
            for round_no in range(1, cfg.boot_wait_rounds + 1):
                if _cancelled(session):
                    return
                _update_status(key, lambda s, r=round_no: s.__setattr__(
                    "status_text", "等待云电脑开机 %d/%d" % (r, cfg.boot_wait_rounds)))
                logs.info(session.display_name,
                          "部分云电脑未开机，等待开机 %d/%d（%ds/轮）"
                          % (round_no, cfg.boot_wait_rounds, cfg.boot_wait_seconds_per_round))
                # 主动开机：对每台未运行的云电脑发送开机指令（已开机的平台会忽略）
                for d in desktop_list:
                    if d.get("useStatusText") == "运行中":
                        continue
                    did = d.get("desktopId", "")
                    dcode = d.get("code") or did
                    pok, pmsg = api.power_on(did)
                    if pok:
                        logs.info(session.display_name, "[%s] 已发送开机指令" % dcode)
                    else:
                        logs.warn(session.display_name, "[%s] 开机指令未确认：%s" % (dcode, pmsg))
                if _interruptible_wait(session, cfg.boot_wait_seconds_per_round):
                    return
                refreshed = api.get_client_list()
                if refreshed:
                    desktop_list = refreshed
                if not any(d.get("useStatusText") != "运行中" for d in desktop_list):
                    break
            if any(d.get("useStatusText") != "运行中" for d in desktop_list):
                raise SessionFailed("部分云电脑未能开机，已超时")

        # 填充设备快照
        def st1(s):
            s.status_text = "连接网关中"
            s.desktops = [{
                "name": d.get("desktopName", ""),
                "code": d.get("desktopCode", ""),
                "desktopId": d.get("desktopId", ""),
                "status": d.get("useStatusText", ""),
            } for d in desktop_list]

        _update_status(key, st1)

        # 逐个连接（单台失败不影响其他，KA-06）
        active_desktops = []
        for desktop in desktop_list:
            if _cancelled(session):
                return
            ok, msg, desktop_info = api.connect(desktop.get("desktopId", ""))
            if ok and desktop_info:
                desktop["desktopInfo"] = desktop_info
                active_desktops.append(desktop)
                _update_desktop_status(key, desktop.get("desktopCode", ""), "连接就绪")
            else:
                code = desktop.get("desktopCode", "")
                logs.fail(session.display_name, "[%s] 连接失败：%s" % (code, msg))
                _update_desktop_status(key, code, "连接出错: " + msg)

        if not active_desktops:
            raise SessionFailed("没有可保活的云电脑")

        _update_status(key, lambda s: s.__setattr__("status_text", "保活运行中"))
        logs.ok(session.display_name, "保活任务启动：每 %d 秒强制重连一次。" % cfg.keep_alive_seconds)

        # 24h 会话强制重建（KA-01）：deadline 由 worker 自行检查
        session_deadline = None
        if cfg.session_restart_minutes > 0:
            session_deadline = time.monotonic() + cfg.session_restart_minutes * 60
            _touch_metrics(key, lambda m: m.__setitem__(
                "nextRestartAt", int(time.time()) + cfg.session_restart_minutes * 60))

        workers = []
        for d in active_desktops:
            t = threading.Thread(
                target=KeepAliveEngine._keepalive_worker,
                args=(session, d, cfg.keep_alive_seconds, session_deadline),
                name="ka-worker-" + key, daemon=True)
            t.start()
            workers.append(t)
        for t in workers:
            t.join()

        if session_deadline is not None and time.monotonic() >= session_deadline and not _cancelled(session):
            logs.info(session.display_name,
                      "触发会话强制重启（周期 %d 分钟），即将重建连接" % cfg.session_restart_minutes)

    # ---------- 单台设备的保活 worker ----------

    _INITIAL_PAYLOAD = None

    @staticmethod
    def _keepalive_worker(session, desktop, keep_alive_seconds, session_deadline):
        key = session.key
        label = session.display_name
        info = desktop.get("desktopInfo")
        code = desktop.get("desktopCode", "")

        if not info or not info.get("clinkLvsOutHost"):
            logs.fail(label, "[%s] 设备连接信息（ClinkLvsOutHost）缺失，跳过该设备保活。" % code)
            return

        if KeepAliveEngine._INITIAL_PAYLOAD is None:
            KeepAliveEngine._INITIAL_PAYLOAD = base64.b64decode(
                "UkVEUQIAAAACAAAAGgAAAAAAAAABAAEAAAABAAAAEgAAAAkAAAAECAAA")
        initial_payload = KeepAliveEngine._INITIAL_PAYLOAD

        consecutive_failures = 0   # 连续异常计数（自适应退避 + 自愈触发依据）
        info_refreshes = 0         # 连接信息刷新失败计数
        cycle_no = 0               # 周期计数
        real_status_failures = 0   # 平台真实状态连续异常计数

        while not _cancelled(session):
            if session_deadline is not None and time.monotonic() >= session_deadline:
                break
            cycle_no += 1

            # ★ 真实状态校验：WebSocket 心跳成功 ≠ 平台认定桌面在用。
            #   若真实会话已死，平台约 60 分钟后自动关机，而心跳仍"假成功"。
            #   每 5 个周期（约 5 分钟）拉一次设备列表核对 useStatusText：
            #   连续 2 次异常即判定保活失效 → 重建会话（重建含自动开机）。
            if cycle_no % 5 == 0:
                try:
                    dlist = session.api.get_client_list()
                except Exception as cex:
                    dlist = None
                    logs.warn(label, "[%s] 真实状态校验请求失败：%s" % (code, cex))
                me = None
                if dlist:
                    me = next((d for d in dlist
                               if str(d.get("desktopId", "")) == str(desktop.get("desktopId", ""))), None)
                real = str((me or {}).get("useStatusText", ""))
                if me is None:
                    real_status_failures += 1
                    logs.warn(label, "[%s] 真实状态校验：设备列表中未找到本桌面（第 %d 次）"
                              % (code, real_status_failures))
                elif real != "运行中":
                    real_status_failures += 1
                    logs.warn(label, "[%s] WebSocket正常但平台真实状态为「%s」（第 %d 次）——"
                              "若持续异常将在约60分钟后被平台关机" % (code, real, real_status_failures))
                else:
                    if real_status_failures:
                        logs.ok(label, "[%s] 平台真实状态已恢复「运行中」" % code)
                    real_status_failures = 0
                if real_status_failures >= 2:
                    logs.fail(label, "[%s] WebSocket心跳正常但平台真实状态持续异常，判定保活失效，"
                              "重建会话（含自动开机）" % code)
                    session.stop.set()
                    threading.Thread(
                        target=KeepAliveEngine._rebuild_session,
                        args=(key, session.account), daemon=True).start()
                    return

            # 每轮动态构造（自愈刷新 desktopInfo 后新配置即刻生效）
            uri = "wss://%s/clinkProxy/%s/MAIN" % (info.get("clinkLvsOutHost"), desktop.get("desktopId", ""))
            origin = ("https://pm.ctyun.cn"
                      if (info.get("osType") or "").lower() == "android"
                      else "https://pc.ctyun.cn")
            cycle_end = time.monotonic() + keep_alive_seconds
            ws = WSClient()
            try:
                logs.info(label, "[%s] === 新周期开始，尝试连接 ===" % code)
                _update_desktop_status(key, code, "正在连接...")
                ws.connect(uri, origin=origin, subprotocol="binary")

                clink = info.get("clinkLvsOutHost", "")
                host, _, port = clink.partition(":")
                if not port:
                    port = "443"
                connect_message = {
                    "type": 1,
                    "ssl": 1,
                    "host": host,
                    "port": port,
                    "ca": info.get("caCert", ""),
                    "cert": info.get("clientCert", ""),
                    "key": info.get("clientKey", ""),
                    "servername": info.get("host", "") + ":" + str(info.get("port", "")),
                    "oqs": 0,
                }
                ws.send_text(json.dumps(connect_message))

                if _interruptible_wait(session, 0.5):
                    break
                ws.send_binary(initial_payload)

                logs.ok(label, "[%s] 连接已就绪，保持 %d 秒..." % (code, keep_alive_seconds))
                _update_desktop_status(key, code, "保活中")
                consecutive_failures = 0
                info_refreshes = 0

                try:
                    KeepAliveEngine._receive_loop(session, ws, desktop, cycle_end)
                    # recv 正常退出：连接被对端关闭或会话停止
                    if _cancelled(session):
                        break
                except WSTimeout:
                    logs.info(label, "[%s] 周期时间到，准备重连..." % code)
                    _touch_metrics(key, lambda m: m.__setitem__(
                        "reconnectCount", m["reconnectCount"] + 1))
            except Exception as ex:
                if _cancelled(session) or isinstance(ex, WSClosed) and session.stop.is_set():
                    break
                consecutive_failures += 1
                logs.fail(label, "[%s] 异常（连续第 %d 次）: %s" % (code, consecutive_failures, ex))
                _update_desktop_status(key, code, "连接异常: " + str(ex))

                # ★ 自愈 1：连续失败 ≥3 → 重新 connect 刷新设备连接信息
                #   （平台轮换 ClinkLvsOutHost / 证书后，原地用旧配置重连必然失败）
                if consecutive_failures >= 3:
                    try:
                        ok, msg, new_info = session.api.connect(desktop.get("desktopId", ""))
                    except Exception as cex:
                        ok, msg, new_info = False, str(cex), None
                    if ok and new_info:
                        desktop["desktopInfo"] = new_info
                        info = new_info
                        info_refreshes = 0
                        logs.ok(label, "[%s] 已刷新设备连接信息（自愈），下轮重连使用新配置" % code)
                    else:
                        info_refreshes += 1
                        logs.warn(label, "[%s] 刷新设备连接信息失败（第 %d 次）：%s"
                                  % (code, info_refreshes, msg))

                # ★ 自愈 2：连续失败 ≥6 或连接信息刷新连续失败 ≥2 → 整会话重建
                #   （重新登录 + 重新获取设备列表，解决登录态过期类根因）
                if consecutive_failures >= 6 or info_refreshes >= 2:
                    logs.fail(label, "[%s] 连续失败 %d 次，自动重建整个保活会话（重新登录）"
                              % (code, consecutive_failures))
                    session.stop.set()
                    threading.Thread(
                        target=KeepAliveEngine._rebuild_session,
                        args=(key, session.account), daemon=True).start()
                    return

                # ★ 自适应退避：5s → 10 → 20 → 40 → 60 封顶，防重连风暴
                wait_s = min(5 * (2 ** max(0, consecutive_failures - 1)), 60)
                logs.info(label, "[%s] %d 秒后重连（自适应退避）" % (code, wait_s))
                if _interruptible_wait(session, wait_s):
                    break
            finally:
                ws.close()
        # worker 结束

    @staticmethod
    def _rebuild_session(key, account):
        """整会话重建兜底：停旧会话 → 重新登录启动。"""
        try:
            KeepAliveEngine.stop(key)
        except Exception:
            pass
        if G.global_stop.is_set():
            return
        try:
            KeepAliveEngine.start(account)
            logs.ok(account.name or key, "保活会话已自动重建（重新登录成功）")
        except Exception as ex:
            logs.fail(account.name or key, "保活会话自动重建失败：%s" % ex)

    # ---------- WebSocket 接收循环 ----------

    @staticmethod
    def _receive_loop(session, ws, desktop, cycle_end):
        key = session.key
        label = session.display_name
        code = desktop.get("desktopCode", "")
        encryptor = Encryption()
        li = session.api.login_info or {}

        while not _cancelled(session):
            if time.monotonic() >= cycle_end:
                raise WSTimeout("周期结束")
            opcode, data = ws.recv_message(deadline=cycle_end)
            if opcode not in (0x1, 0x2) or not data:
                continue

            if data[:4] == b"REDQ":
                logs.ok(label, "[%s] -> 收到保活校验" % code)
                response = encryptor.execute(data)
                ws.send_binary(response)
                logs.ok(label, "[%s] -> 发送保活响应成功" % code)
                now = int(time.time())

                def touch(m, now=now):
                    m["heartbeatSuccess"] = m["heartbeatSuccess"] + 1
                    m["lastHeartbeatAt"] = now

                _touch_metrics(key, touch)
                continue

            try:
                for info in SendInfo.from_buffer(data):
                    if info.type == 103:
                        # 与 C# 完全一致的 JSON 拼装（无空格）
                        payload = ('{"type":1,"userName":"' + str(li.get("userName", "")) +
                                   '","userInfo":"","userId":' + str(li.get("userId", 0)) + '}')
                        frame = SendInfo(118, payload.encode("utf-8")).to_buffer(True)
                        ws.send_binary(frame)
            except Exception as ex:
                logs.warn(label, "[%s] 消息解析失败: %s" % (code, ex))
                now = int(time.time())

                def touch_fail(m, now=now):
                    m["heartbeatFailed"] = m["heartbeatFailed"] + 1
                    m["lastHeartbeatAt"] = now

                _touch_metrics(key, touch_fail)


# ---------- 账号主键（对应 C# AccountKey.cs） ----------

def normalize_key(user: str) -> str:
    return user.strip() if user and user.strip() else ""


def resolve_key(name_or_user, accounts):
    """先按 User 精确匹配，再按 Name 匹配；无则 None。"""
    if not name_or_user or not name_or_user.strip() or accounts is None:
        return None
    text = name_or_user.strip()
    for a in accounts:
        if normalize_key(a.user) == text:
            return normalize_key(a.user)
    for a in accounts:
        if (a.name or "").strip() == text:
            return normalize_key(a.user)
    return None


def find_account(name_or_user, accounts):
    if not name_or_user or not name_or_user.strip() or accounts is None:
        return None
    text = name_or_user.strip()
    for a in accounts:
        if normalize_key(a.user) == text:
            return a
    for a in accounts:
        if (a.name or "").strip() == text:
            return a
    return None


# ---------- 保活重启调度（对应 C# KeepAliveRestarter.cs） ----------

class KeepAliveRestarter:
    _restart_at = None          # 计划重启时刻（Unix 秒；通道 A 兑换成功后 +120s）
    _last_file_proc = 0.0       # 已消费的信号文件最后写时间（防重复触发）

    @classmethod
    def schedule_restart(cls, at_unix: int):
        cls._restart_at = at_unix
        logs.warn("[系统]", "已安排保活重启：%s" % datetime.fromtimestamp(at_unix).strftime("%Y-%m-%d %H:%M:%S"))

    @classmethod
    def restart_all(cls):
        import os
        for key in list(G.active_workers.keys()):
            try:
                acc = find_account(key, G.config.accounts)
                if acc is not None:
                    KeepAliveEngine.start(acc)
            except Exception as ex:
                logs.fail("[系统]", "重启保活失败（%s）：%s" % (key, ex))

    @classmethod
    def run_loop(cls, stop_event: threading.Event):
        """10s 轮询：处理内存计划 + 脚本写入的重启信号文件。"""
        while not stop_event.is_set() and not G.global_stop.is_set():
            try:
                now = time.time()
                if cls._restart_at is not None and now >= cls._restart_at:
                    cls._restart_at = None
                    cls.restart_all()
                    logs.warn("[系统]", "已按计划在 2 分钟后重启保活")

                path = store.Paths.restart_at_path
                if os.path.exists(path):
                    mtime = os.path.getmtime(path)
                    if mtime > cls._last_file_proc:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read().strip()
                        try:
                            sec = int(content)
                        except ValueError:
                            sec = -1
                        if sec > 0:
                            if sec <= time.time():
                                cls.restart_all()
                                logs.warn("[系统]", "检测到脚本写入的重启信号，执行保活重启")
                                try:
                                    os.remove(path)
                                except OSError:
                                    pass
                                cls._last_file_proc = time.time()
                        else:
                            cls._last_file_proc = mtime  # 解析失败：标记已消费
            except Exception as ex:
                logs.fail("[系统]", "重启调度异常：%s" % ex)

            if stop_event.wait(10) or G.global_stop.wait(0):
                break


# ---------- 保活状态监视（停止/心跳异常 → fail 日志自动联动飞书告警） ----------

class KeepAliveStatusMonitor:
    """每分钟巡检账号保活状态：

    - 运行中 → 停止：立即告警（fail 日志 → 飞书自动推送）；
    - 运行中但连续心跳失败达阈值：告警一次，恢复后自动重置；
    - 服务启动时已处于停止状态的账号不重复告警（只监视运行中转变）。
    """

    CHECK_INTERVAL = 60          # 巡检间隔（秒）
    FAILURE_ALERT_THRESHOLD = 5  # 连续心跳失败告警阈值
    OFFLINE_CONFIRM_ROUNDS = 2   # 离线告警需连续确认轮数（防瞬时中断误报）

    _last_running = {}           # key -> 上一轮是否运行中
    _alerted_failures = set()    # 已告警连续失败的账号（恢复后移除）
    _last_desktop_online = {}    # (key, 桌面code) -> 上一轮是否在线
    _offline_streak = {}         # (key, 桌面code) -> 连续离线轮数
    _ONLINE_KEYWORDS = ("保活", "运行", "就绪", "连接", "在线")   # 与前端一致
    _OFFLINE_KEYWORDS = ("关机", "离线", "连接异常")              # 明确的关机/离线/链路中断标志

    @classmethod
    def run_loop(cls, stop_event):
        # 首轮等待 30 秒，让自动启动的保活先就位，避免启动瞬间误判
        if stop_event.wait(30) or G.global_stop.wait(0):
            return
        while not stop_event.is_set() and not G.global_stop.is_set():
            try:
                cls._check_once()
            except Exception as ex:
                logs.fail("保活监视", "监视循环异常：" + str(ex))
            if stop_event.wait(cls.CHECK_INTERVAL):
                break

    @classmethod
    def _check_once(cls):
        with G._gate:
            accounts = list(G.config.accounts) if G.config else []
        for acc in accounts:
            key = cls._key(acc.user)
            info = G.account_statuses.get(key)
            is_running = bool(info and info.is_running)
            cf = int((info.metrics.get("consecutiveFailures", 0) or 0)) if info else 0
            prev = cls._last_running.get(key)

            # 1) 运行中 → 停止：立即告警
            if prev is True and not is_running:
                err = (info.metrics.get("lastError") or "") if info else ""
                detail = ("，最近错误：" + str(err)[:150]) if err else ""
                logs.fail("保活监视", "账号 %s（%s）的保活已停止，云电脑主机失去保活%s"
                          % (acc.name or "未命名", logs.mask_user(acc.user), detail))

            # 2) 运行中但心跳连续失败达阈值：告警一次
            if is_running and cf >= cls.FAILURE_ALERT_THRESHOLD and key not in cls._alerted_failures:
                cls._alerted_failures.add(key)
                logs.fail("保活监视", "账号 %s（%s）连续心跳失败 %d 次，保活链路可能异常"
                          % (acc.name or "未命名", logs.mask_user(acc.user), cf))
            elif is_running and cf < cls.FAILURE_ALERT_THRESHOLD and key in cls._alerted_failures:
                cls._alerted_failures.discard(key)
                logs.ok("保活监视", "账号 %s（%s）心跳已恢复正常"
                        % (acc.name or "未命名", logs.mask_user(acc.user)))

            # 3) 云电脑主机关机/离线/连接中断：状态由在线转为离线 → 保活失败告警
            #    离线须连续 2 轮巡检（≥60 秒）才告警：瞬时"连接异常"通常在自适应退避重连中自愈，
            #    直接告警会产生误报（实例：19:36 剑账号 10054 瞬断 11 秒后自愈，却被报"保活失败"）
            for d in (info.desktops if info else []):
                code = d.get("code") or d.get("name") or "?"
                dkey = (key, code)
                text = str(d.get("status") or "")
                # 离线关键词优先判定（"连接异常"含"连接"二字，须先排除）
                offline = any(k in text for k in cls._OFFLINE_KEYWORDS)
                online = (not offline) and any(k in text for k in cls._ONLINE_KEYWORDS)
                if not online and not offline:
                    online = True  # 未知过渡态（如"开机中"）不误报，视为在线
                last_online = cls._last_desktop_online.get(dkey)
                streak = cls._offline_streak.get(dkey, 0)
                if offline:
                    streak += 1
                else:
                    streak = 0
                cls._offline_streak[dkey] = streak
                if last_online is True and not online and streak >= cls.OFFLINE_CONFIRM_ROUNDS:
                    logs.fail("保活监视", "账号 %s（%s）的云电脑「%s」已关机/离线（状态：%s），保活失败"
                              % (acc.name or "未命名", logs.mask_user(acc.user),
                                 d.get("name") or code, text or "未知"))
                elif last_online is False and online:
                    logs.ok("保活监视", "账号 %s（%s）的云电脑「%s」已恢复在线（%s）"
                            % (acc.name or "未命名", logs.mask_user(acc.user),
                               d.get("name") or code, text or "未知"))
                cls._last_desktop_online[dkey] = online

            cls._last_running[key] = is_running

    @staticmethod
    def _key(user: str):
        return normalize_key(user)
