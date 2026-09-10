# -*- coding: utf-8 -*-
"""
自动更新：从 GitHub 仓库拉取最新代码，保留本机数据。

设计要点：
- 只覆盖「代码文件」，账号/任务/兑换配置/设备码/日志/虚拟环境一律不动
- 比对远端 main 分支最新提交 sha 与本地记录，相同则不做任何写操作
- 下载失败不影响现有服务运行（所有异常都转成 {ok:False, error:...}）
- 压缩包校验：大小上限 / CRC 完整性 / 结构与路径安全（防 zip-slip）；
  先解压到临时目录、校验通过后再逐文件替换，失败回滚，绝不半写
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

import logs

REPO_OWNER = "laichidashu"
REPO_NAME = "CtYun-KeepAlive-Web"
BRANCH = "main"

API_LATEST = "https://api.github.com/repos/%s/%s/commits/%s" % (REPO_OWNER, REPO_NAME, BRANCH)
ZIP_URL = "https://codeload.github.com/%s/%s/zip/refs/heads/%s" % (REPO_OWNER, REPO_NAME, BRANCH)
STATE_FILE = "update_state.json"

# 下载体积上限（GitHub 源码包正常仅几 MB，超限视为异常，拒绝写盘）
_MAX_ZIP_BYTES = 200 * 1024 * 1024

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
        raw = resp.read()
    if len(raw) > _MAX_ZIP_BYTES:
        raise ValueError("压缩包体积 %d 字节超过上限 %d 字节，疑似异常响应"
                         % (len(raw), _MAX_ZIP_BYTES))
    return raw


def _member_name_safe(rel: str) -> bool:
    """zip 成员相对路径安全校验（防 zip-slip）：拒绝绝对路径、盘符、
    「..」目录穿越段、反斜杠与空字节。"""
    if not rel:
        return False
    if "\x00" in rel:
        return False
    if rel.replace("\\", "/") != rel:
        return False  # 反斜杠在 Windows 上是路径分隔符，可能逃逸
    if rel.startswith("/") or rel.startswith("~"):
        return False
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            return False
        if len(p) >= 2 and p[1] == ":":  # Windows 盘符（C:/...）
            return False
    return True


def _validate_and_list(raw: bytes):
    """校验压缩包完整性与结构。返回 (ZipFile, [(成员名, 相对路径)])。

    校验失败直接抛 ValueError（由调用方转成安全退出，不写任何文件）：
    - zip 格式无法解析 / CRC 校验损坏（testzip）
    - 顶层目录不一致或不含任何文件（结构异常）
    - 任何成员名含路径穿越段
    """
    zf = zipfile.ZipFile(io.BytesIO(raw))
    try:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError("压缩包 CRC 校验失败，成员损坏：%s" % bad)
    except ValueError:
        raise
    except Exception as ex:
        raise ValueError("压缩包校验异常：%s" % ex)

    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise ValueError("压缩包为空")
    top = names[0].split("/")[0]
    if not top or "/" in top:
        raise ValueError("压缩包顶层目录结构异常：%r" % top)
    entries = []
    for n in names:
        if not n.startswith(top + "/"):
            raise ValueError("压缩包内存在多个顶层目录，结构异常：%s" % n)
        rel = n[len(top) + 1:]
        if not rel:
            continue
        if not _member_name_safe(rel):
            raise ValueError("压缩包成员路径不安全，拒绝解压：%s" % n)
        entries.append((n, rel))
    if not entries:
        raise ValueError("压缩包顶层目录下没有任何文件")
    return zf, entries


def perform_update(timeout: float = 120) -> dict:
    """执行更新：下载远端压缩包 → 校验 → 暂存 → 逐文件替换（非受保护文件）。

    返回 {ok, updated, changed, remote, error}。
    安全策略：所有校验在写目标文件之前完成；先完整解压到临时目录，
    再逐文件替换，任何一步失败立即回滚（尽力而为），绝不留下半写状态。
    """
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
    stage_root = tempfile.mkdtemp(prefix="ctyun_update_")
    backup_root = tempfile.mkdtemp(prefix="ctyun_backup_")
    try:
        try:
            zf, entries = _validate_and_list(raw)
        except Exception as ex:
            return {"ok": False, "updated": False, "changed": [], "remote": rem["sha"],
                    "error": "压缩包校验未通过：" + str(ex)}

        # 阶段 1：全部解压到临时目录（此时未触碰任何目标文件）
        try:
            for name, rel in entries:
                if _is_protected(rel):
                    continue
                dst_stage = os.path.join(stage_root, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst_stage), exist_ok=True)
                with open(dst_stage, "wb") as f:
                    f.write(zf.read(name))
        except Exception as ex:
            return {"ok": False, "updated": False, "changed": [], "remote": rem["sha"],
                    "error": "压缩包解压失败：" + str(ex)}

        # 阶段 2：逐文件替换目标（先备份原文件，失败时回滚尽力而为）
        replaced = []  # (dst, 备份路径或 None=新文件)
        try:
            for name, rel in entries:
                if _is_protected(rel):
                    continue
                dst_stage = os.path.join(stage_root, rel.replace("/", os.sep))
                dst = os.path.join(root, rel.replace("/", os.sep))
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst_stage, "rb") as f:
                        data = f.read()
                    if os.path.exists(dst):
                        with open(dst, "rb") as f:
                            if f.read() == data:
                                continue  # 内容一致，无需替换
                        backup = os.path.join(backup_root, "%08d" % len(replaced))
                        with open(dst, "rb") as src, open(backup, "wb") as f:
                            f.write(src.read())
                    else:
                        backup = None
                    with open(dst, "wb") as f:
                        f.write(data)
                    replaced.append((dst, backup))
                    changed.append(rel)
                except Exception as ex:
                    raise RuntimeError("替换文件失败 %s：%s" % (rel, ex))
        except Exception as ex:
            # 回滚：恢复备份的原文件，删除本次新建的文件（尽力而为）
            for dst, backup in reversed(replaced):
                try:
                    if backup is not None:
                        with open(backup, "rb") as f:
                            with open(dst, "wb") as g:
                                g.write(f.read())
                    else:
                        os.remove(dst)
                except Exception:
                    pass
            logs.fail("更新", "更新写入失败，已回滚改动：" + str(ex))
            return {"ok": False, "updated": False, "changed": [], "remote": rem["sha"],
                    "error": "更新写入失败（已回滚）：" + str(ex)}
    finally:
        try:
            import shutil
            shutil.rmtree(stage_root, ignore_errors=True)
            shutil.rmtree(backup_root, ignore_errors=True)
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
