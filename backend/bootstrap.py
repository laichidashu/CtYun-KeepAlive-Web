# -*- coding: utf-8 -*-
"""
启动前置环境自检：服务运行前自动检查/安装依赖，全部就绪才放行启动。
- 检查 requests / pillow / DrissionPage / ddddocr
- 缺失则自动 pip 安装（默认源失败自动换清华镜像重试）
- 安装后复检，仍缺则退出码 1（bat 据此中止启动）
仅用标准库，可被 启动服务.bat / 手动命令行调用。
"""
import importlib
import subprocess
import sys

REQUIRED = [
    ("requests", "requests"),
    ("PIL", "pillow"),
    ("DrissionPage", "DrissionPage"),
    ("ddddocr", "ddddocr"),
]
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _missing() -> list:
    out = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pkg)
    return out


def main() -> int:
    v = sys.version_info
    print("[bootstrap] Python %s (%s)" % (".".join(map(str, v[:3])), sys.executable))
    if v < (3, 10):
        print("[bootstrap][WARN] Python %d.%d 版本较旧，建议 3.10+，过低可能导致依赖安装失败" % (v.major, v.minor))

    missing = _missing()
    if not missing:
        print("[bootstrap] 环境检查通过：requests / pillow / DrissionPage / ddddocr 已就绪")
        return 0

    print("[bootstrap] 缺失依赖：%s，开始自动安装（首次安装约需 1-3 分钟，请耐心等待）..." % ", ".join(missing))
    cmds = [
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"] + missing,
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-i", MIRROR] + missing,
    ]
    for i, cmd in enumerate(cmds):
        if i:
            print("[bootstrap] 默认源安装未成功，改用清华镜像重试 ...")
        try:
            rc = subprocess.call(cmd)
        except Exception as ex:
            print("[bootstrap] pip 启动失败：%s" % ex)
            rc = 1
        if rc == 0:
            break

    still = _missing()
    if still:
        print("[bootstrap][ERROR] 安装后复检仍缺失：%s" % ", ".join(still))
        print("[bootstrap] 请检查网络后手动执行：")
        print('    "%s" -m pip install %s' % (sys.executable, " ".join(still)))
        return 1
    print("[bootstrap] 依赖安装完成并通过复检")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
