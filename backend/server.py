# -*- coding: utf-8 -*-
"""
CtYun-KeepAlive-Web —— Python 版入口（对应 C# Program.cs Main）。

启动顺序：
  1. Paths.Initialize（数据目录/文件路径/脚本目录）
  2. 容错加载 accounts.json（文件缺失/损坏 → .bak → 环境变量 → 空配置）
  3. 后处理：展示名兜底、设备码解析、脚本目录校正、日志历史上限
  4. 后台循环：定时任务调度器（cron）+ 保活重启调度
  5. 自动启动已有账号保活（登录验证后启动）
  6. Web 服务监听 0.0.0.0:PORT（默认 8080）
"""
import os
import signal
import sys
import threading
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


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """客户端主动断开（SSE 关闭、超时等）属正常现象，不打印堆栈。"""

    daemon_threads = True

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


def main():
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
    port = int(os.environ.get("PORT", "8080") or "8080")
    # 端口占用预检：Windows 下 SO_REUSEADDR 允许重复绑定，会导致新旧实例并存
    import socket as _socket
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _probe.bind(("0.0.0.0", port))
        _probe.close()
    except OSError:
        logs.fail("系统", "端口 %d 已被占用（可能已有实例在运行），本次启动退出。"
                  "如需重启请先结束旧进程。" % port)
        return
    server = QuietThreadingHTTPServer(("0.0.0.0", port), httpd.Handler)
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
    finally:
        logs.write_line("[系统] 正在停止所有后台任务...", logs.LEVEL_WARN, "系统")
        _stop_event.set()
        G.global_stop.set()
        server.server_close()
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
