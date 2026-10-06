# -*- coding: utf-8 -*-
"""
CtYun-KeepAlive-Web —— Python 版入口（对应 C# Program.cs Main）。

启动顺序：
  1. Paths.Initialize（数据目录/文件路径/脚本目录）
  2. 容错加载 accounts.json（文件缺失/损坏 → .bak → 环境变量 → 空配置）
  3. 后处理：展示名兜底、设备码解析、脚本目录校正、日志历史上限
  4. 后台循环：定时任务调度器（cron）+ 保活重启调度
  5. 自动启动已有账号保活（登录验证后启动）
  6. Web 服务监听 0.0.0.0:PORT（默认 8081，可用环境变量 PORT 覆盖）
"""
import atexit
import os
import platform
import re
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpd  # noqa: E402
import jobs  # noqa: E402
import keepalive  # noqa: E402
import logs  # noqa: E402
import store  # noqa: E402
from store import ConfigStore, G, Paths  # noqa: E402

VERSION = "2.0.0-py"

_stop_event = threading.Event()
_START_MONOTONIC = time.monotonic()   # 进程启动时刻，用于退出时统计已运行秒数
_startup_aborted_reason = ""          # 早退原因（非空 → 退出日志用它，避免误报「被外部终止」）


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """客户端主动断开（SSE 关闭、超时等）属正常现象，不打印堆栈。"""

    daemon_threads = True
    # 关键：Windows 下 SO_REUSEADDR 允许两个实例绑定同一端口，会导致
    # 新旧实例并存、互斥锁/任务状态分裂。必须禁用，绑定失败直接退出。
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _lan_ips():
    """枚举本机局域网 IPv4（排除回环），用于在启动日志中给出可访问地址。"""
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))  # 不实际发包，仅取路由出口 IP
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


# ---- pidfile：记录运行实例 PID，供端口冲突时给出明确提示 ----

def _pidfile_path() -> str:
    """pidfile 放数据目录（与其他运行时文件同目录，如 ctyun_restart_at）。"""
    return os.path.join(Paths.data_dir, "server.pid")


def _write_pidfile() -> None:
    try:
        with open(_pidfile_path(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as ex:
        logs.warn("系统", "写入 pidfile 失败（不影响服务运行）：" + str(ex))


def _remove_pidfile(force: bool = False) -> bool:
    """清理 pidfile，返回是否删除成功。

    默认（force=False）只删本进程自己写的那份（或缺失/损坏的），绝不删别人的：
    main() 早退（如「已有实例运行中」）后解释器退出仍会执行 atexit，若无条件
    删除，会把活着那个实例的 pidfile 抹掉 → 后续重复启动识别不出已有实例 →
    配合端口顺延出现「两个服务实例并存」（互斥锁/任务状态分裂）。

    force=True 用于「陈旧残留」场景（调用方已确认该 PID 不再存活）：允许删
    别人的，但内部仍会再确认一次进程确实不存活，活着则拒绝——防止误删把
    bae77e7 修掉的问题又放回来。
    """
    try:
        recorded = _read_pidfile()
        if recorded in (0, os.getpid()):
            os.remove(_pidfile_path())  # 缺失/损坏或本进程自己的，删除无害
            return True
        if not force:
            return False  # 属于其它实例（可能仍活着），绝不动
        if _process_alive(recorded):
            return False  # 防呆：即便 force，活实例的 pidfile 也绝不删
        os.remove(_pidfile_path())
        return True
    except OSError:
        return False


def _process_alive(pid: int) -> bool:
    """判断进程是否存活（仅用标准库）。

    注意：Windows 上不能用 os.kill(pid, 0) 探测——CPython 在 Windows 把
    os.kill 实现为 OpenProcess(PROCESS_ALL_ACCESS) + TerminateProcess，
    对同用户进程会真的将其杀死（退出码即 sig）。故 Windows 走
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess。
    """
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False  # 进程不存在或无权限（无权限≈非本服务实例）
            try:
                exit_code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    # POSIX：os.kill(pid, 0) 只探测不发信号
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权限发信号
    except OSError:
        return False


def _read_pidfile() -> int:
    """读取 pidfile 记录的 PID；文件缺失/损坏返回 0。"""
    try:
        with open(_pidfile_path(), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


# 默认监听端口。之所以从 8080 改成 8081：本机有常驻的外部 node.exe 会抢占
# 8080 的回环地址（127.0.0.1:8080），实测出现过两次（PID 9784、1968）。它只占
# 回环、不占通配，于是「我们的服务绑得上 0.0.0.0:8080」但「用户打开
# localhost:8080 却命中 node 的 401 页面」。改成 8081 直接避开。
# 仍可用环境变量 PORT 覆盖；仍保留 PORT+1..PORT+10 的顺延兜底。
DEFAULT_PORT = 8081
PORT_FALLBACK_TRIES = 10   # 首选端口被占用时，向后顺延尝试 PORT+1 .. PORT+10


def _loopback_free(port: int) -> bool:
    """探测回环地址 127.0.0.1:port 是否还能绑定（判断「本机访问」是否属于我们）。

    Windows 上「通配 0.0.0.0:PORT」与「具体 127.0.0.1:PORT」的绑定可以共存
    （只有双方都带 SO_EXCLUSIVEADDRUSE 才互斥），而本机访问走的是更具体的
    回环绑定——所以即使 0.0.0.0 bind 成功，用户打开 localhost:PORT 也可能
    命中别人的进程（实测：外部 node.exe 占着 127.0.0.1:8080 就是这种情况）。
    故正式 bind 之前必须先探测回环；探测失败即判定该端口不可用并顺延。

    探测 socket 不带 SO_REUSEADDR：处于 LISTEN 或 TIME_WAIT 的地址都会探测
    失败（偏保守，宁可顺延也不冒「绑上了却访问到别人」的风险）。
    """
    import socket
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def _bind_with_fallback(preferred_port: int, bind_fn=None, tries: int = PORT_FALLBACK_TRIES,
                        probe_fn=None):
    """绑定端口：首选端口不可用时向后顺延最多 tries 个（PORT+1 .. PORT+tries）。

    每个候选端口先做回环探测（probe_fn），探测失败即跳过——即使随后
    0.0.0.0 能绑成功也不能用，否则用户在本机会访问到别人的进程。

    bind_fn / probe_fn 仅为测试注入替身而留（默认分别是构造
    QuietThreadingHTTPServer 与 _loopback_free）。
    返回 (server, port, skipped)：skipped 为「因回环被占而跳过」的端口列表；
    全不可用时返回 (None, preferred_port, skipped)。

    注意：allow_reuse_address 恒为 False——Windows 下 SO_REUSEADDR 会让新旧
    实例并存、互斥锁与任务状态分裂，绝不能为了「绑得上」而放开它。

    取舍：探测与正式 bind 之间存在毫秒级的时间窗竞争（探测刚关、别人抢占）。
    这是可接受的——进程内间隔极短，且真正要防的是「长期占着端口的外来进程」；
    真被抢占时表现为 bind 抛 OSError，顺延到下一个候选即可。
    """
    if bind_fn is None:
        def bind_fn(p):
            return QuietThreadingHTTPServer(("0.0.0.0", p), httpd.Handler)
    if probe_fn is None:
        probe_fn = _loopback_free

    skipped = []
    for offset in range(0, max(0, int(tries)) + 1):
        candidate = preferred_port + offset
        if not probe_fn(candidate):
            skipped.append(candidate)  # 回环被占：本机访问会命中别人，跳过
            continue
        try:
            return bind_fn(candidate), candidate, skipped
        except OSError:
            continue  # 通配地址被占用，顺延下一个
    return None, preferred_port, skipped


# 脱敏规则：手机号（11 位 1xx）+ URL（query/fragment 整体剥离，验证码/手机号多在 query 里）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_URL_RE = re.compile(r"https?://[^\s'\"<>)}\]]+")


def _sanitize(text: str) -> str:
    """日志脱敏：URL 只留 scheme+host+path（剥离 query/fragment），手机号走掩码。

    只削敏感字段，异常类型、函数名、行号、普通消息全部保留，便于排障。
    """
    if not text:
        return text
    try:
        def strip_query(m):
            url = m.group(0)
            for sep in ("?", "#"):
                idx = url.find(sep)
                if idx >= 0:
                    url = url[:idx]
            return url
        text = _URL_RE.sub(strip_query, text)
        return _PHONE_RE.sub(lambda m: logs.mask_user(m.group(1)), text)
    except Exception:
        return text


def _format_exception(exc_type, exc_value, exc_traceback) -> str:
    """把异常压成单行文本：仅含类型/消息/堆栈，敏感字段（手机号/URL query）已脱敏。

    必须脱敏的原因：这条日志会走 logs.fail → 可能经飞书推到外部群；而异常消息
    里可能带手机号（如设备码文件路径）或完整 URL（含 mobilePhone/captchaCode）。
    """
    try:
        lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    except Exception:
        lines = ["%s: %s" % (getattr(exc_type, "__name__", exc_type), exc_value)]
    flat = " | ".join(x.strip() for x in "".join(lines).splitlines() if x.strip())
    flat = flat or "%s" % getattr(exc_type, "__name__", exc_type)
    return _sanitize(flat)[:2000]


def _thread_excepthook(args) -> None:
    """daemon 线程（cron / 保活重启 / 状态监视 / autostart）未捕获异常 → 写入应用日志。

    默认行为只打到 stderr；start.ps1 启动时不保留 stderr，线程会静默死亡、
    应用日志毫无痕迹。这里统一落地为 ERROR 级日志。
    """
    name = getattr(getattr(args, "thread", None), "name", "") or "未知线程"
    try:
        logs.fail("系统", "线程「%s」未捕获异常，已终止：%s"
                  % (name, _format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    except Exception:
        pass


def _sys_excepthook(exc_type, exc_value, exc_traceback) -> None:
    """主线程未捕获异常 → 应用日志留痕（KeyboardInterrupt / SystemExit 属正常退出，不记）。"""
    try:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return
    except Exception:
        pass
    try:
        logs.fail("系统", "主线程未捕获异常，进程即将退出：%s"
                  % _format_exception(exc_type, exc_value, exc_traceback))
    except Exception:
        pass


def _install_exception_hooks() -> None:
    """注册全局异常钩子，保证「死因」一定落进应用日志。"""
    try:
        threading.excepthook = _thread_excepthook  # Python 3.8+
    except Exception:
        pass
    sys.excepthook = _sys_excepthook


def _set_startup_aborted(reason: str) -> None:
    """记录启动阶段早退的真实原因（供退出日志使用）。"""
    global _startup_aborted_reason
    _startup_aborted_reason = reason


def _exit_logger() -> None:
    """atexit：进程退出留痕（WARN 级，与正常停机的 INFO「已退出」区分）。

    注册时机很关键：atexit 是后进先出（LIFO），logs 模块在 import 时
    （server.py 顶部 import logs）已注册 close_file_handle；本钩子在 main()
    里注册、晚于它，因此退出时先执行本钩子、后关文件句柄，日志能写进去。
    """
    try:
        seconds = int(time.monotonic() - _START_MONOTONIC)
        pid = os.getpid()
        if _startup_aborted_reason:
            # 启动阶段主动早退：原因已知，不能写成「被外部终止」误导排障
            logs.warn("系统", "进程退出（PID %d）：已运行 %d 秒（%s）"
                      % (pid, seconds, _startup_aborted_reason))
        elif _stop_event.is_set():
            logs.warn("系统", "进程退出（PID %d）：已运行 %d 秒（已走正常停机流程）" % (pid, seconds))
        else:
            logs.warn("系统", "进程退出（PID %d）：已运行 %d 秒，未走正常停机流程"
                      "（可能被外部终止 / 崩溃 / 控制台被关闭），请检查上方是否有异常日志"
                      % (pid, seconds))
    except Exception:
        pass


def _install_exit_hook() -> None:
    """注册退出钩子：先记退出日志，再清理 pidfile（LIFO：后注册者先执行）。"""
    atexit.register(_exit_logger)
    atexit.register(_remove_pidfile)


def main():
    _install_exception_hooks()
    _install_exit_hook()
    logs.write_line("版本：v " + VERSION, logs.LEVEL_INFO, "系统")

    # 1. 路径先行
    Paths.initialize()
    logs.set_file_dir(Paths.logs_dir)  # 日志持久化到 <数据目录>/logs/（按天滚动，保留30天）
    G.config = None

    # 2. 容错加载（文件缺失/损坏 -> .bak -> 环境变量 -> 空配置）
    cfg = ConfigStore.load(
        Paths.accounts_path,
        store.AppConfig.from_dict,
        lambda: httpd.load_accounts_from_environment() or store.AppConfig())
    G.config = cfg

    # 3. 后处理
    for account in cfg.accounts:
        account.name = httpd.first_not_empty(account.name, account.user)
        account.device_code = httpd.resolve_device_code(account, Paths.data_dir)
    Paths.refresh_scripts_dir(cfg.scripts_dir)
    logs.Log.set_history_limit(cfg.log_history_size)
    logs.set_retention_days(cfg.log_retention_days)

    # 4. 后台长循环（不使用任何框架 HostedService，直接线程承载）
    threading.Thread(target=jobs.CronScheduler.run_loop, args=(_stop_event,),
                     name="cron-scheduler", daemon=True).start()
    threading.Thread(target=keepalive.KeepAliveRestarter.run_loop, args=(_stop_event,),
                     name="ka-restarter", daemon=True).start()
    threading.Thread(target=keepalive.KeepAliveStatusMonitor.run_loop, args=(_stop_event,),
                     name="ka-status-monitor", daemon=True).start()

    logs.write_line("[系统] 后台调度已启动：定时任务调度器（cron）+ 保活重启调度 + 保活状态监视",
                    logs.LEVEL_INFO, "系统")

    jobs.JobService.initialize()

    # 5. 自动启动已有账号保活
    for account in cfg.accounts:
        threading.Thread(target=_autostart_account, args=(account,),
                         name="autostart", daemon=True).start()

    # 6. Web 服务
    preferred = int(os.environ.get("PORT", str(DEFAULT_PORT)) or str(DEFAULT_PORT))

    # 6.1 本服务实例已在运行 → 直接退出（pidfile 属于那个活着的实例，绝不删）
    old_pid = _read_pidfile()
    if old_pid and old_pid != os.getpid() and _process_alive(old_pid):
        _set_startup_aborted("早退：已有实例运行中（PID %d），本次未启动服务" % old_pid)
        logs.fail("系统", "已有实例运行中（PID %d，记录于 server.pid），请勿重复启动。"
                  "请勿重复双击「启动服务」；如需重启请先结束旧进程（PID %d）。"
                  "本次启动退出。" % (old_pid, old_pid))
        return

    # 6.2 端口被其它程序占用（如外部 node 进程）→ 顺延尝试，而不是直接死掉
    server, port, skipped = _bind_with_fallback(preferred)
    if server is None:
        _set_startup_aborted("早退：端口 %d~%d 均不可用，本次未启动服务"
                             % (preferred, preferred + PORT_FALLBACK_TRIES))
        logs.fail("系统", "端口 %d~%d 均不可用：未发现存活的本服务实例"
                  "（可能旧实例刚退出留下 TIME_WAIT，或其它程序占用了这些端口"
                  "%s）。请稍候重试，或用 netstat -ano | findstr :%d 结束占用进程。"
                  "本次启动退出。"
                  % (preferred, preferred + PORT_FALLBACK_TRIES,
                     "，其中 %s 的回环地址被占用" % "、".join(str(p) for p in skipped) if skipped else "",
                     preferred))
        # 走到这里说明没有存活的本服务实例：pidfile 若还在就是陈旧残留，
        # 必须清掉——否则该 PID 被系统回收给别的进程后，新实例会被「已有实例
        # 运行中」挡住，进而触发看门狗崩溃循环保护。仅在确认已死后强制删除。
        stale_pid = _read_pidfile()
        if stale_pid and stale_pid != os.getpid() and not _process_alive(stale_pid):
            if _remove_pidfile(force=True):
                logs.info("系统", "已清理陈旧 pidfile（原 PID %d 已不存在）" % stale_pid)
        return
    if skipped:
        logs.warn("系统", "端口 %s 的回环地址（127.0.0.1）已被其他进程占用："
                  "通配绑定（0.0.0.0）虽仍可能成功，但本机访问 localhost 会命中该进程，"
                  "已跳过这些端口。" % "、".join(str(p) for p in skipped))
    if port != preferred:
        logs.warn("系统", "端口 %d 不可用，已自动改用 %d，"
                  "请用 http://localhost:%d 访问。" % (preferred, port, port))
    # 绑定成功后再写 pidfile：此时才确定本实例是端口持有者
    _write_pidfile()
    logs.write_line("[系统] Web 服务已启动，监听地址：http://localhost:%d" % port,
                    logs.LEVEL_INFO, "系统")
    for ip in _lan_ips():
        logs.write_line("[系统] 局域网访问：http://%s:%d （其他设备请用此地址）" % (ip, port),
                        logs.LEVEL_INFO, "系统")
    logs.write_line("[系统] 提示：若浏览器无法访问，请检查 1) Windows 防火墙是否放行 TCP %d；"
                    "2) 浏览器/系统代理是否拦截了该地址。" % port,
                    logs.LEVEL_INFO, "系统")

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        # 非 Ctrl+C 的异常退出：先留痕再走 finally（否则日志里毫无痕迹）
        logs.fail("系统", "Web 服务循环异常退出：%s: %s" % (type(ex).__name__, ex))
    finally:
        logs.write_line("[系统] 正在停止所有后台任务...", logs.LEVEL_WARN, "系统")
        _stop_event.set()
        G.global_stop.set()
        server.server_close()
        _remove_pidfile()  # 正常退出清理 pidfile（异常路径由 atexit 兜底）
        logs.write_line("[系统] 已退出", logs.LEVEL_INFO, "系统")


def _handle_sig(signum, frame):
    raise KeyboardInterrupt


def _autostart_account(account):
    """启动自检：登录验证 → 已绑定则启动保活，否则提示重新绑定。"""
    label = account.name or account.user
    api = __import__("ctyun_api").CtYunApi(account.device_code)
    logs.info(label, "[启动自检] 正在登录验证...")

    if api.login(account.user, account.password):
        if api.login_info.get("bondedDevice", False):
            keepalive.KeepAliveEngine.start(account)
            return
        logs.warn(label, "[启动自检] 该设备未绑定，请在控制面板删除重新添加绑定！")
        key = keepalive.normalize_key(account.user)
        status = G.statuses_get_or_add(key)
        status.status_text = "等待验证码"
    else:
        logs.fail(label, "[启动自检] 自动登录失败，跳过该账号。")


if __name__ == "__main__":
    main()
