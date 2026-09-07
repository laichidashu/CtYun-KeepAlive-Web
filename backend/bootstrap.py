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

    print("[bootstrap] 缺失依赖：%s，开始自动安装（首次安装约需 1-5 分钟，请耐心等待）..." % ", ".join(missing))
    # 国内网络环境优先镜像源；pip 本身限制重试，失败快速切换下一个源
    sources = [
        ("清华镜像", MIRROR),
        ("阿里云镜像", "https://mirrors.aliyun.com/pypi/simple/"),
        ("官方源", "https://pypi.org/simple/"),
    ]
    rc = 1
    for name, index_url in sources:
        cmd = [sys.executable, "-m", "pip", "install",
               "--disable-pip-version-check", "--retries", "2", "--timeout", "15",
               "-i", index_url] + missing
        print("[bootstrap] 正在从「%s」安装 ..." % name)
        try:
            rc = subprocess.call(cmd)
        except Exception as ex:
            print("[bootstrap] pip 启动失败：%s" % ex)
            rc = 1
        if rc == 0:
            break
        print("[bootstrap] 「%s」安装未成功，切换下一个源 ..." % name)

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
