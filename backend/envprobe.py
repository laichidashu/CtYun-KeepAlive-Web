# -*- coding: utf-8 -*-
"""
环境自检（对应 C# EnvironmentProbe.cs）。
探测 Python 解释器、关键依赖（DrissionPage/ddddocr/requests）、脚本文件与（建议性）Chromium。
chromium 为 advisory，不计入 allOk。不抛异常，任何单项失败仅标记 ok=false。
"""
import os
import platform
import shutil
import subprocess

import store
from store import G, Paths

_NAME = platform.system()


def _exists_on_path(name: str) -> bool:
    return shutil.which(name) is not None


def _detect_python() -> str:
    exe = (G.config.python_executable if G.config else "") or ""
    exe = exe.strip()
    if exe:
        return exe
    for cand in ("python3", "python"):
        if _exists_on_path(cand):
            return cand
    return "python3"


def _run_capture(fileName, args):
    try:
        proc = subprocess.run(
            [fileName] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10,
            creationflags=(0x08000000 if _NAME == "Windows" else 0))  # CREATE_NO_WINDOW
        output = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
        return proc.returncode == 0, output.strip()
    except Exception:
        return False, ""


def _get_python_version(python: str) -> str:
    if not python:
        return ""
    ok, output = _run_capture(python, ["--version"])
    return output if ok else ""


def _python_module_ok(python: str, module: str) -> bool:
    if not python:
        return False
    ok, _ = _run_capture(python, ["-c", "import " + module])
    return ok


def _detect_chromium() -> bool:
    candidates = (["chromium", "chromium-browser", "google-chrome", "chrome", "msedge"]
                  if _NAME == "Windows"
                  else ["chromium", "chromium-browser", "google-chrome"])
    for c in candidates:
        if _exists_on_path(c):
            return True
    if _NAME == "Windows":
        # 补充探测常见安装路径（Edge 为 Chromium 内核，DrissionPage 可直接驱动）
        # 注意：expandvars 不支持含括号的变量名（如 ProgramFiles(x86)），需显式读取
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        la = os.environ.get("LocalAppData", os.path.expanduser(r"~\AppData\Local"))
        file_candidates = [
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(la, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in file_candidates:
            if os.path.isfile(p):
                return True
    return False


def _make_item(name, display, ok, detail, fix):
    return {"name": name, "displayName": display, "ok": ok, "detail": detail, "fixCommand": fix}


def probe() -> dict:
    python = _detect_python()
    version = _get_python_version(python)

    items = []

    items.append(_make_item(
        "python", "Python 解释器",
        bool(python and python.strip()),
        ("未找到" if not python else python + ((" " + version) if version else "")),
        "请安装 Python 3.8+ 并加入 PATH"))

    for mod in ("DrissionPage", "ddddocr", "requests"):
        module_ok = _python_module_ok(python, mod)
        items.append(_make_item(
            mod, mod, module_ok,
            "已安装" if module_ok else "未找到",
            "pip install DrissionPage ddddocr requests"))

    sai = os.path.exists(Paths.ai_chat_script)
    items.append(_make_item(
        "script_aichat", "AI对话脚本", sai,
        Paths.ai_chat_script if sai else "未找到 " + Paths.ai_chat_script,
        "将 ai_chat_task.py 放到脚本目录：" + Paths.scripts_dir))

    spc = os.path.exists(Paths.pc_hang_script)
    items.append(_make_item(
        "script_pchang", "云电脑挂机脚本", spc,
        Paths.pc_hang_script if spc else "未找到 " + Paths.pc_hang_script,
        "将 pc_hang_task.py 放到脚本目录：" + Paths.scripts_dir))

    chromium = _detect_chromium()
    items.append(_make_item(
        "chromium", "Chromium（建议）", chromium,
        "已检测到" if chromium else "未找到（建议安装，浏览器任务更稳定）",
        "apt install chromium 或下载 Chromium 并加入 PATH"))

    all_ok = all(it["ok"] for it in items if it["name"] != "chromium")

    return {
        "allOk": all_ok,
        "pythonPath": python,
        "pythonVersion": version,
        "checkedAt": int(__import__("time").time()),
        "items": items,
    }


# ---------- 一键自动安装缺失依赖 ----------

import threading

import logs

_install_gate = threading.Lock()
_install_running = False
_INSTALLABLE = (("DrissionPage", "DrissionPage"), ("ddddocr", "ddddocr"), ("requests", "requests"))
_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def is_installing() -> bool:
    return _install_running


def _run_streaming(cmd):
    """运行安装命令，输出逐行写入系统日志。返回是否退出码为 0。"""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=(0x08000000 if _NAME == "Windows" else 0))  # CREATE_NO_WINDOW
    except Exception as ex:
        logs.fail("环境", "启动 pip 失败：" + str(ex))
        return False
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                logs.info("环境", line)
        proc.wait(timeout=900)
        return proc.returncode == 0
    except Exception as ex:
        logs.fail("环境", "pip 执行异常：" + str(ex))
        try:
            proc.kill()
        except Exception:
            pass
        return False


def _install_worker(python: str, pkgs):
    """后台安装线程：默认源 → 失败换清华镜像重试 → 复检。"""
    global _install_running
    try:
        logs.info("环境", "开始安装缺失依赖：%s（解释器 %s）" % (" ".join(pkgs), python))
        ok = _run_streaming([python, "-m", "pip", "install"] + pkgs)
        if not ok:
            logs.warn("环境", "默认源安装未成功，改用清华镜像重试……")
            ok = _run_streaming(
                [python, "-m", "pip", "install", "-i", _MIRROR] + pkgs)
        if not ok:
            logs.fail("环境", "依赖安装失败，请查看上方日志，或按各检查项的修复命令手动处理")
            return
        still = [mod for mod, _pkg in _INSTALLABLE if not _python_module_ok(python, mod)]
        if still:
            logs.fail("环境", "安装命令执行完毕，但复检仍缺：%s（可能安装到了错误解释器，请检查设置中的 Python 路径）" % ", ".join(still))
            return
        logs.ok("环境", "依赖安装完成并通过复检 ✓ 可重新运行环境自检确认")
    finally:
        with _install_gate:
            _install_running = False


def install_missing() -> dict:
    """探测缺失依赖并异步启动安装。返回 {started, msg, missing}。"""
    global _install_running
    with _install_gate:
        if _install_running:
            return {"started": False, "msg": "安装任务正在进行中，请稍候（进度见实时日志）", "missing": []}
        python = _detect_python()
        if not python:
            return {"started": False, "msg": "未找到可用的 Python 解释器", "missing": []}
        missing = [mod for mod, _pkg in _INSTALLABLE if not _python_module_ok(python, mod)]
        if not missing:
            return {"started": False, "msg": "依赖已齐全，无需安装", "missing": []}
        _install_running = True
    threading.Thread(target=_install_worker, args=(python, missing),
                     name="env-install", daemon=True).start()
    return {"started": True, "msg": "安装任务已启动", "missing": missing}
