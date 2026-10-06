# -*- coding: utf-8 -*-
"""
服务看门狗：拉起 backend/server.py，并在其退出后自动重启。

设计要点：
- 子进程继承 stdout/stderr（不做任何重定向），输出与直接跑 server.py 一致
- 子进程退出 → 记录退出码 + 本次运行时长，等 5 秒后自动重启
- 崩溃循环保护：60 秒内连续退出 3 次 → 停止重启并报错退出，避免无限刷日志
- 收到 Ctrl+C / SIGINT / SIGTERM → 交给子进程一起退出，不再重启
- 判活：Windows 走 ctypes OpenProcess；**绝不用 os.kill(pid, 0)**
  （CPython 在 Windows 把它实现为 OpenProcess + TerminateProcess，会真杀进程）
- 零第三方依赖

用法：python backend/watchdog.py（start.ps1 已改为调用本文件）
"""
import atexit
import os
import platform
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logs  # noqa: E402
from store import Paths  # noqa: E402
from updater import WATCHDOG_ENV_VAR  # noqa: E402

RESTART_DELAY_SECONDS = 5    # 子进程退出后等待多久再重启
CRASH_WINDOW_SECONDS = 60    # 崩溃循环判定窗口（秒）
CRASH_THRESHOLD = 3          # 窗口内退出次数达到该值 → 判定为反复崩溃，停止重启
# 只有「本次运行很短」才计入崩溃计数：健康长跑（如挂机 3600 秒）之后的偶发
# 抖动不该被算成启动即崩溃，否则看门狗会永久放弃一台其实健康的机器
CRASH_MIN_RUN_SECONDS = 30
# 单次运行超过该时长视为健康，清零崩溃计数（长期跑挂机任务必然远超此值）
CRASH_RESET_AFTER_SECONDS = 300
STOP_GRACE_SECONDS = 8       # 请求停止后，允许子进程自行退出的宽限时间
POLL_SECONDS = 0.2           # 轮询间隔（便于及时响应停止信号）

_stopping = False            # 已收到停止信号：不再重启
_stop_deadline = 0.0         # 宽限截止时刻，到期仍未退出则强杀
_proc = None                 # 当前子进程


def base_dir() -> str:
    """工程根目录（backend/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def server_script() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


def process_alive(pid: int) -> bool:
    """判断进程是否存活（与 server.py:_process_alive 同思路，仅用标准库）。

    Windows：OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess。
    POSIX：os.kill(pid, 0) 只探测、不发信号。
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
                return False  # 进程不存在或无权限
            try:
                exit_code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权限发信号
    except OSError:
        return False


def recent_exits(exit_times, now: float = None, window: float = None) -> list:
    """取崩溃窗口内的退出时刻列表（供崩溃循环判定与测试使用）。

    只保留 [now-window, now] 区间内的记录：既剔除窗口外的旧记录（防止长期
    flap 累积导致误判），也剔除未来时间戳（时钟回拨/传入错误时 now-t < 0）。
    """
    now = time.time() if now is None else now
    window = CRASH_WINDOW_SECONDS if window is None else window
    return [t for t in exit_times if 0 <= now - t <= window]


def update_exit_times(exit_times, seconds: int, now: float = None) -> list:
    """按本次运行时长更新崩溃计数（纯函数，便于注入式测试）。

    - seconds >= CRASH_RESET_AFTER_SECONDS：健康运行 → 清零计数；
    - CRASH_MIN_RUN_SECONDS <= seconds < CRASH_RESET_AFTER_SECONDS：中间地带，
      既不算健康也不算快速崩溃 → 保留既有计数、不计入本次；
    - seconds < CRASH_MIN_RUN_SECONDS：快速退出 → 计入本次。
    返回已裁剪到窗口内（并剔除未来时间戳）的新列表。
    """
    now = time.time() if now is None else now
    if seconds >= CRASH_RESET_AFTER_SECONDS:
        return []
    if seconds < CRASH_MIN_RUN_SECONDS:
        return recent_exits(list(exit_times) + [now], now)
    return recent_exits(exit_times, now)


# ---- 看门狗自身的单实例保护 ----
# server 有 server.pid 保护，看门狗若没有，双击启动会起第二个看门狗、
# 各带一个服务子进程（实测出现过 PID 混乱）。故同样用 pidfile 互斥。

def _pidfile_path() -> str:
    """看门狗 pidfile（数据目录/watchdog.pid，与 server.pid 同目录）。"""
    return os.path.join(Paths.data_dir, "watchdog.pid")


def _read_pidfile() -> int:
    """读取 pidfile 中的 PID；缺失/损坏返回 0。"""
    try:
        with open(_pidfile_path(), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _write_pidfile() -> None:
    try:
        with open(_pidfile_path(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as ex:
        logs.warn("看门狗", "写入 watchdog.pid 失败（不影响服务运行）：" + str(ex))


def _remove_pidfile() -> None:
    """只删本进程自己写的那份 pidfile，绝不删别人的。"""
    try:
        recorded = _read_pidfile()
        if recorded not in (0, os.getpid()):
            return
        os.remove(_pidfile_path())
    except OSError:
        pass


def _install_signal_handlers() -> None:
    """Ctrl+C / SIGINT / SIGTERM：置停止标志并请求子进程退出（不再重启）。"""

    def handler(signum, frame):
        global _stopping, _stop_deadline
        if _stopping:
            return
        _stopping = True
        _stop_deadline = time.monotonic() + STOP_GRACE_SECONDS
        logs.warn("看门狗", "收到停止信号（%s），正在停止服务，不再自动重启。" % signum)
        _request_child_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK 仅 Windows 存在
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def _request_child_stop() -> None:
    """请求子进程退出。

    POSIX：直接发 SIGINT，交给 server.py 的信号处理走正常停机流程。
    Windows：子进程与本进程共享控制台，会自行收到 Ctrl+C；这里不抢着强杀，
    只等宽限期（STOP_GRACE_SECONDS）过后由主循环兜底 terminate。
    """
    global _proc
    if _proc is None or _proc.poll() is not None:
        return
    if platform.system() != "Windows":
        try:
            _proc.send_signal(signal.SIGINT)
        except Exception:
            pass


def _spawn():
    """启动服务子进程（继承 stdout/stderr，不新建进程组以便 Ctrl+C 一起收到）。

    注入 CTYUN_WATCHDOG=1：子进程（及其里的 updater.restart_service）据此
    知道「重启归看门狗管」，面板点更新时不再自己 spawn server.py，避免
    脱离监管 + 触发崩溃循环保护。
    """
    cmd = [sys.executable or "python", server_script()]
    env = os.environ.copy()
    env[WATCHDOG_ENV_VAR] = "1"
    return subprocess.Popen(cmd, cwd=base_dir(), env=env)


def main() -> int:
    global _proc

    Paths.initialize()
    logs.set_file_dir(Paths.logs_dir)

    # 单实例保护：已有看门狗在跑 → 直接退出，避免两个看门狗各带一个服务子进程
    existing = _read_pidfile()
    if existing and existing != os.getpid() and process_alive(existing):
        logs.fail("看门狗", "已有看门狗运行中（PID %d，记录于 watchdog.pid），请勿重复启动。"
                  "如需重启请先结束旧进程（PID %d）。本次启动退出。" % (existing, existing))
        return 1
    _write_pidfile()
    atexit.register(_remove_pidfile)

    logs.info("看门狗", "看门狗已启动（PID %d）：服务异常退出将自动重启，"
              "退出码记录见下方日志。" % os.getpid())
    _install_signal_handlers()

    exit_times = []
    restarts = 0
    while True:
        started = time.monotonic()
        restarts_text = "（第 %d 次重启）" % restarts if restarts else ""
        logs.info("看门狗", "正在启动服务：%s %s" % (server_script(), restarts_text))
        try:
            _proc = _spawn()
        except Exception as ex:
            logs.fail("看门狗", "启动服务进程失败：%s" % ex)
            return 1

        proc = _proc
        # 轮询等待：既能及时响应停止信号，也能在宽限到期后兜底强杀
        while proc.poll() is None:
            if _stopping and _stop_deadline and time.monotonic() >= _stop_deadline:
                logs.warn("看门狗", "服务在 %d 秒宽限期内未自行退出，强制结束。" % STOP_GRACE_SECONDS)
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            time.sleep(POLL_SECONDS)

        try:
            code = proc.wait(timeout=STOP_GRACE_SECONDS)
        except Exception:
            code = proc.returncode
        seconds = int(time.monotonic() - started)
        _proc = None

        logs.warn("看门狗", "服务进程已退出（退出码 %s，本次运行 %d 秒，PID %d）"
                  % (code, seconds, proc.pid))

        if _stopping:
            logs.info("看门狗", "看门狗已停止，服务不再重启。")
            return 0

        # 只有「短时间内反复快速退出」才算崩溃循环；健康长跑会清零计数
        if seconds >= CRASH_RESET_AFTER_SECONDS:
            logs.info("看门狗", "本次运行 %d 秒（≥ %d 秒），视为健康运行，已清零崩溃计数。"
                      % (seconds, CRASH_RESET_AFTER_SECONDS))
        elif seconds >= CRASH_MIN_RUN_SECONDS:
            logs.info("看门狗", "本次运行 %d 秒（≥ %d 秒，不算快速崩溃），"
                      "不计入崩溃计数，直接重启。" % (seconds, CRASH_MIN_RUN_SECONDS))
        exit_times = update_exit_times(exit_times, seconds)
        window = exit_times
        if len(window) >= CRASH_THRESHOLD:
            logs.fail("看门狗", "服务在 %d 秒内连续快速退出 %d 次（每次运行均短于 %d 秒），"
                      "判定为短时间内反复退出，已停止自动重启。请查看上方服务日志定位原因"
                      "（或手动运行 backend\\server.py 复现）。"
                      % (CRASH_WINDOW_SECONDS, len(window), CRASH_MIN_RUN_SECONDS))
            return 1

        restarts += 1
        logs.info("看门狗", "%d 秒后自动重启服务（已重启 %d 次）..." % (RESTART_DELAY_SECONDS, restarts))
        deadline = time.monotonic() + RESTART_DELAY_SECONDS
        while time.monotonic() < deadline:
            if _stopping:
                logs.info("看门狗", "收到停止信号，取消本次重启。")
                return 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
