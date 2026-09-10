# -*- coding: utf-8 -*-
"""
Python 任务脚本运行器（对应 C# ScriptRunner.cs + ProcessTree.cs）。
- 严格按清单设置环境变量（APP_USER/APP_PASSWORD/DEVICECODE/CTYUN_* 等）
- stdout → Info 日志、stderr → Warn 日志（实时行级读取）
- 超时 → 杀整棵进程树；Summary 取输出尾部 500 字
- Linux 下补扫 /proc 回收被 reparent 的 chromium 残留
"""
import os
import platform
import shutil
import signal
import subprocess
import threading
import time

import logs
import store
from store import Paths
from logs import mask_user

_TIMEOUT_KILL_WAIT = 5

# 正在运行的脚本进程登记表：key = (账号, 任务类型) 组合键 → subprocess.Popen
# browserMutexMode=PerType 时同账号 ai_chat 与 pc_hang 可并行，必须用组合键
# 区分，否则第二个进程会覆盖第一个的登记，停止时另一进程泄漏。
_RUNNING_LOCK = threading.Lock()
_RUNNING = {}


def make_run_key(account_user: str, job_type: str = "") -> str:
    """构造进程登记键：job_type 非空时用「账号:类型」组合键，否则退化为旧键
    （兼容非任务调用者，保持旧行为）。"""
    user = account_user or ""
    jtype = (job_type or "").strip()
    if not user:
        return ""
    return user + ":" + jtype if jtype else user


def detect_python() -> str:
    """优先配置项，否则探测 python3 / python。"""
    exe = (G_config_python() or "").strip()
    if exe:
        return exe
    for cand in ("python3", "python"):
        if shutil.which(cand):
            return cand
    return "python3"


def G_config_python() -> str:
    g = store.G
    return g.config.python_executable if g.config else ""


def _kill_tree_windows(pid: int):
    # taskkill /T /F：整树强杀，绝不全局扫进程名（避免误杀用户浏览器）
    os.system("taskkill /T /F /PID %d >nul 2>&1" % pid)


def _kill_tree_posix(root_pid: int):
    # Linux：/proc 构建 pid→ppid 映射，BFS 收集后代后 SIGKILL
    mapping = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % name, "r") as f:
                    stat = f.read()
                close_paren = stat.rfind(")")
                rest = stat[close_paren + 1:].strip().split(" ")
                if len(rest) >= 2:
                    mapping[int(name)] = int(rest[1])
            except Exception:
                pass
    except Exception:
        pass

    # BFS 收集后代
    descendants = set()
    queue = [root_pid]
    while queue:
        cur = queue.pop(0)
        for pid, ppid in mapping.items():
            if ppid == cur and pid not in descendants:
                descendants.add(pid)
                queue.append(pid)

    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception as ex:
            logs.warn("进程", "杀残留进程 %d 失败：%s" % (pid, ex))


def kill_tree(proc: subprocess.Popen):
    """杀掉进程及整棵子进程树（best-effort，异常吞掉记日志）。"""
    if proc is None:
        return
    try:
        if platform.system() == "Windows":
            _kill_tree_windows(proc.pid)
        else:
            # start_new_session=True 使子进程自成进程组，可整组回收
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception as ex:
        logs.warn("进程", "Kill(entireProcessTree) 失败：" + str(ex))

    try:
        proc.wait(timeout=_TIMEOUT_KILL_WAIT)
    except Exception:
        try:
            proc.kill()
        except Exception as ex:
            logs.warn("进程", "二次 Kill 失败：" + str(ex))

    # Linux 补充：回收被 reparent 的 chromium 残留
    if platform.system() == "Linux":
        try:
            _kill_tree_posix(proc.pid)
        except Exception as ex:
            logs.warn("进程", "Linux 进程树扫描异常：" + str(ex))


def _trim_summary(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= 500:
        return text
    return text[-500:]


def _register(key: str, proc: subprocess.Popen) -> None:
    """登记正在运行的脚本进程，供「停止任务」终止。"""
    if not key:
        return
    with _RUNNING_LOCK:
        _RUNNING[key] = proc


def _unregister(key: str) -> None:
    if not key:
        return
    with _RUNNING_LOCK:
        _RUNNING.pop(key, None)


def kill_running(key: str) -> bool:
    """终止指定键名（账号）下正在运行的脚本进程（整树强杀）。返回是否找到并终止。"""
    if not key:
        return False
    with _RUNNING_LOCK:
        proc = _RUNNING.get(key)
    if proc is None:
        return False
    try:
        kill_tree(proc)
        return True
    except Exception:
        return False


def run(account, script_path: str, hang_seconds: int, timeout_minutes: int,
        job_type: str = "") -> dict:
    """运行脚本。返回 {exitCode, timedOut, startFailed, summary}。

    job_type：任务类型（如 ai_chat / pc_hang），用于区分同账号并行任务
    的进程登记键；留空时保持旧行为（仅按账号登记）。
    """
    result = {"exitCode": 0, "timedOut": False, "startFailed": False, "summary": ""}
    python_exe = detect_python()
    tag = ("挂机" if script_path == Paths.pc_hang_script else "AI对话") + "[" + mask_user(account.user) + "]"
    run_key = make_run_key(account.user, job_type)

    env = os.environ.copy()
    env.update({
        "APP_USER": account.user or "",
        "APP_PASSWORD": account.password or "",
        "DEVICECODE": account.device_code or "",
        "RUNNING_IN_DOCKER": "true" if store.is_container() else "false",
        "CTYUN_DATA_DIR": Paths.data_dir,
        "CTYUN_REDEEM_CONFIG": Paths.redeem_config_path,
        "CTYUN_HANG_SECONDS": str(hang_seconds),
        "CTYUN_RESTART_AT_FILE": Paths.restart_at_path,
        "PYTHONUNBUFFERED": "1",   # 关键：强制无缓冲，日志实时流出
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })

    try:
        proc = subprocess.Popen(
            [python_exe, script_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=Paths.data_dir, env=env,
            creationflags=(subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0),
            start_new_session=(platform.system() != "Windows"))
    except Exception as ex:
        logs.fail(tag, "启动脚本进程失败：" + str(ex))
        result["exitCode"] = -2
        result["startFailed"] = True
        result["summary"] = "启动失败：" + str(ex)
        return result

    # 登记运行中的进程，供「停止任务」真正终止脚本
    _register(run_key, proc)
    out_tail = []  # 尾部缓冲（上限 64 行，超出丢最旧）
    err_tail = []
    _TAIL_MAX = 64

    def pump(stream, tail, is_err):
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                (logs.warn if is_err else logs.info)(tag, line)
                tail.append(line)
                if len(tail) > _TAIL_MAX:
                    tail.pop(0)
        except Exception:
            pass  # 读取被中断（进程被杀）忽略
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_tail, False), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_tail, True), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout_minutes * 60
    timed_out = False
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.2)

    if timed_out:
        result["timedOut"] = True
        result["exitCode"] = -1
        kill_tree(proc)
        t_out.join(timeout=2)
        t_err.join(timeout=2)
    else:
        try:
            result["exitCode"] = proc.returncode if proc.returncode is not None else -1
        except Exception:
            result["exitCode"] = -1
        t_out.join(timeout=3)
        t_err.join(timeout=3)

    combined = "\n".join(out_tail) + "\n" + "\n".join(err_tail)
    result["summary"] = _trim_summary(combined)
    _unregister(run_key)
    return result
