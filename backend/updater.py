# -*- coding: utf-8 -*-
"""
自动更新：从 GitHub 仓库拉取最新代码，保留本机数据。

设计要点：
- 只覆盖「代码文件」，账号/任务/兑换配置/设备码/日志/虚拟环境一律不动
- 比对远端 main 分支最新提交 sha 与本地记录，相同则不做任何写操作
- 下载失败不影响现有服务运行（所有异常都转成 {ok:False, error:...}）
- 可通过命令行调用：python backend/updater.py [--check|--update]
    退出码：0=无更新 / 2=已更新 / 1=出错
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO_OWNER = "laichidashu"
REPO_NAME = "CtYun-KeepAlive-Web"
BRANCH = "main"

API_LATEST = "https://api.github.com/repos/%s/%s/commits/%s" % (REPO_OWNER, REPO_NAME, BRANCH)
ZIP_URL = "https://codeload.github.com/%s/%s/zip/refs/heads/%s" % (REPO_OWNER, REPO_NAME, BRANCH)
STATE_FILE = "update_state.json"

# 永不覆盖：用户数据与运行环境（相对工程根目录，目录以 / 结尾）
PROTECTED_DIRS = ("devices/", "logs/", ".venv/", "venv/", ".git/", "__pycache__/",
                  "backup/", ".update_backup/")
PROTECTED_FILES = ("accounts.json", "accounts.json.bak", "jobs.json", "jobs_history.json",
                   "redeem_config.json", "redeem_config.json.bak", "startup.log",
                   "update_state.json")
PROTECTED_SUFFIX = (".log", ".bak")
PROTECTED_PREFIX = ("ctyun_cookies_",)


def base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_protected(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    if not rel:
        return True
    for d in PROTECTED_DIRS:
        if rel.startswith(d) or ("/" + d) in ("/" + rel):
            return True
    name = os.path.basename(rel)
    if name in PROTECTED_FILES:
        return True
    if name.endswith(PROTECTED_SUFFIX):
        return True
    if name.startswith(PROTECTED_PREFIX):
        return True
    return False


def _read_state() -> dict:
    path = os.path.join(base_dir(), STATE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(data: dict):
    path = os.path.join(base_dir(), STATE_FILE)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def local_ref() -> str:
    """本地代码版本：优先「上次同步的远端版本」记录，其次 git HEAD。

    之所以以记录为准：本机 git 历史与 GitHub 上的提交可能不同源（历史被重写时
    sha 天然不同），只有「上次同步到哪个远端 commit」才是准确的比对基准。
    """
    remembered = str(_read_state().get("sha", "") or "")
    if remembered:
        return remembered
    root = base_dir()
    if os.path.isdir(os.path.join(root, ".git")):
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root,
                stderr=subprocess.DEVNULL, timeout=10)
            sha = out.decode("utf-8", "replace").strip()
            if sha:
                return sha[:12]
        except Exception:
            pass
    return str(_read_state().get("sha", "") or "")


def remote_ref(timeout: float = 20) -> dict:
    """查询远端 main 分支最新提交。返回 {ok, sha, message, date, error}。"""
    try:
        req = urllib.request.Request(
            API_LATEST, headers={"User-Agent": "ctyun-keepalive",
                                 "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        commit = data.get("commit", {})
        return {
            "ok": True,
            "sha": str(data.get("sha", ""))[:12],
            "message": (commit.get("message", "") or "").splitlines()[0] if commit.get("message") else "",
            "date": (commit.get("committer", {}) or {}).get("date", ""),
            "error": "",
        }
    except Exception as ex:
        return {"ok": False, "sha": "", "message": "", "date": "", "error": str(ex)}


def check_update(timeout: float = 20) -> dict:
    """是否有新版本。返回 {ok, hasUpdate, local, remote, message, date, error}。"""
    rem = remote_ref(timeout)
    loc = local_ref()
    if not rem["ok"]:
        return {"ok": False, "hasUpdate": False, "local": loc, "remote": "",
                "message": "", "date": "", "error": rem["error"]}
    has = bool(rem["sha"]) and rem["sha"] != loc
    return {"ok": True, "hasUpdate": has, "local": loc, "remote": rem["sha"],
            "message": rem["message"], "date": rem["date"], "error": ""}


def _download_zip(timeout: float = 120) -> bytes:
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "ctyun-keepalive"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def perform_update(timeout: float = 120) -> dict:
    """执行更新：下载远端压缩包 → 覆盖非受保护文件。返回 {ok, updated, changed, remote, error}。"""
    rem = remote_ref(timeout=20)
    if not rem["ok"]:
        return {"ok": False, "updated": False, "changed": [], "remote": "",
                "error": "无法连接 GitHub：" + rem["error"]}
    loc = local_ref()
    if rem["sha"] and rem["sha"] == loc:
        return {"ok": True, "updated": False, "changed": [], "remote": rem["sha"], "error": ""}

    try:
        raw = _download_zip(timeout)
    except Exception as ex:
        return {"ok": False, "updated": False, "changed": [], "remote": rem["sha"],
                "error": "下载失败：" + str(ex)}

    root = base_dir()
    changed = []
    tmp_root = tempfile.mkdtemp(prefix="ctyun_update_")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                return {"ok": False, "updated": False, "changed": [], "remote": rem["sha"],
                        "error": "压缩包为空"}
            # 去掉压缩包内的顶层目录
            top = names[0].split("/")[0]
            for n in names:
                rel = n[len(top) + 1:] if n.startswith(top + "/") else n
                if not rel or _is_protected(rel):
                    continue
                data = zf.read(n)
                dst = os.path.join(root, rel.replace("/", os.sep))
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.exists(dst):
                        with open(dst, "rb") as f:
                            if f.read() == data:
                                continue
                    with open(dst, "wb") as f:
                        f.write(data)
                    changed.append(rel)
                except Exception:
                    continue
    except Exception as ex:
        return {"ok": False, "updated": False, "changed": changed, "remote": rem["sha"],
                "error": "解压失败：" + str(ex)}
    finally:
        try:
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass

    _write_state({"sha": rem["sha"], "updatedAt": int(time.time()),
                  "message": rem["message"], "date": rem["date"]})
    if not changed:
        # 远端与本地代码内容一致（历史不同源但文件相同）：记为已同步，不算更新
        return {"ok": True, "updated": False, "changed": [], "remote": rem["sha"],
                "message": rem["message"], "error": ""}
    return {"ok": True, "updated": True, "changed": changed, "remote": rem["sha"],
            "message": rem["message"], "error": ""}


def restart_service(delay: float = 1.0):
    """重启服务：另起一个守护进程，等当前进程退出后重新拉起 server.py。"""
    root = base_dir()
    server = os.path.join(root, "backend", "server.py")
    py = sys.executable or "python"
    helper = (
        "import os,subprocess,sys,time\n"
        "pid=%d\n"
        "for _ in range(240):\n"
        "    try:\n"
        "        os.kill(pid,0)\n"
        "    except OSError:\n"
        "        break\n"
        "    time.sleep(0.5)\n"
        "time.sleep(0.5)\n"
        "cf=0\n"
        "if os.name=='nt':\n"
        "    cf=0x00000008|0x00000200\n"
        "subprocess.Popen([r'%s', r'%s'], cwd=r'%s', creationflags=cf)\n"
        % (os.getpid(), py, server, root))
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    try:
        subprocess.Popen([py, "-c", helper], cwd=root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = "--check" if "--check" in argv else "--update"
    if mode == "--check":
        res = check_update()
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    res = perform_update()
    print(json.dumps({k: v for k, v in res.items() if k != "changed"}, ensure_ascii=False)
          + (" | changed=%d" % len(res.get("changed", []))))
    if not res["ok"]:
        return 1
    return 2 if res["updated"] else 0


if __name__ == "__main__":
    sys.exit(main())
