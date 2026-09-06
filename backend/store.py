# -*- coding: utf-8 -*-
"""
路径解析 / 配置存取 / 全局状态（对应 C# Paths.cs + ConfigStore.cs + Program.cs GlobalState）。
所有落盘路径一律由 Paths 派生，保证容器内外都不写卷外路径。
"""
import json
import os
import threading

import logs


def is_container() -> bool:
    return os.path.exists("/.dockerenv")


# 工程根目录：backend/ 的上一级（scripts/ 与 CtYun/wwwroot/ 都在这里）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Paths:
    """数据目录与文件路径常量。server.main() 启动时 initialize() 一次。"""

    data_dir = ""
    accounts_path = ""      # 主配置（accounts.json）
    jobs_path = ""          # jobs.json
    jobs_history_path = ""  # jobs_history.json
    redeem_config_path = "" # redeem_config.json
    restart_at_path = ""    # 延迟重启信号文件（脚本写、后端读）
    devices_dir = ""        # 设备码持久化目录
    logs_dir = ""           # 本地日志文件目录（按天滚动）
    scripts_dir = ""        # 脚本目录
    ai_chat_script = ""     # ai_chat_task.py 完整路径
    pc_hang_script = ""     # pc_hang_task.py 完整路径

    @classmethod
    def initialize(cls):
        data_dir = os.environ.get("CTYUN_DATA_DIR", "").strip()
        if not data_dir:
            data_dir = "/app/data" if is_container() else BASE_DIR
        cls.data_dir = data_dir
        os.makedirs(cls.data_dir, exist_ok=True)

        config_path = os.environ.get("CTYUN_CONFIG", "").strip()
        cls.accounts_path = config_path if config_path else os.path.join(cls.data_dir, "accounts.json")

        cls.jobs_path = os.path.join(cls.data_dir, "jobs.json")
        cls.jobs_history_path = os.path.join(cls.data_dir, "jobs_history.json")
        cls.redeem_config_path = os.path.join(cls.data_dir, "redeem_config.json")
        cls.restart_at_path = os.path.join(cls.data_dir, "ctyun_restart_at")

        cls.devices_dir = os.path.join(cls.data_dir, "devices")
        os.makedirs(cls.devices_dir, exist_ok=True)

        cls.logs_dir = os.path.join(cls.data_dir, "logs")
        os.makedirs(cls.logs_dir, exist_ok=True)

        cls.refresh_scripts_dir("")

    @classmethod
    def refresh_scripts_dir(cls, configured: str):
        """配置的 scriptsDir 存在时使用之，否则回落到工程根下 scripts/。"""
        if configured and configured.strip() and os.path.isdir(configured.strip()):
            cls.scripts_dir = configured.strip()
        else:
            cls.scripts_dir = os.path.join(BASE_DIR, "scripts")
        cls.ai_chat_script = os.path.join(cls.scripts_dir, "ai_chat_task.py")
        cls.pc_hang_script = os.path.join(cls.scripts_dir, "pc_hang_task.py")

    @staticmethod
    def device_code_file(user: str) -> str:
        safe = "default" if not user or not user.strip() else user.strip()
        return os.path.join(Paths.data_dir, ".devicecode_" + safe)

    @staticmethod
    def backup(path: str) -> str:
        return path + ".bak"

    @staticmethod
    def temp(path: str) -> str:
        return path + ".tmp"


class AppConfig:
    """主配置（accounts.json）。字段名与 .NET 版逐字一致，旧文件零破坏。"""

    def __init__(self):
        self.accounts = []                    # List[AccountConfig]
        self.keep_alive_seconds = 60
        self.admin_password = "admin"
        self.session_restart_minutes = 1440   # 24h 会话强制重建，0=关闭
        self.session_token_hours = 12
        self.python_executable = ""
        self.scripts_dir = ""
        self.ai_chat_timeout_minutes = 15
        self.pc_hang_timeout_minutes = 100
        self.pc_hang_seconds = 4800
        self.boot_wait_rounds = 3
        self.boot_wait_seconds_per_round = 60
        self.min_healthy_session_seconds = 60
        self.browser_mutex_mode = "Global"    # Global | PerType
        self.poll_interval_seconds = 5
        self.log_history_size = 500
        self.feishu_webhook = ""              # 飞书自定义机器人 Webhook（空=关闭推送）
        self.feishu_secret = ""               # 飞书加签密钥（未加签留空）
        self.feishu_app_id = ""               # 飞书自建应用 App ID（与 webhook 二选一）
        self.feishu_app_secret = ""           # 飞书自建应用 App Secret
        self.feishu_chat_id = ""              # 目标群 chat_id（空=自动取机器人所在第一个群）

    def to_dict(self):
        return {
            "accounts": [a.to_dict() for a in self.accounts],
            "keepAliveSeconds": self.keep_alive_seconds,
            "adminPassword": self.admin_password,
            "sessionRestartMinutes": self.session_restart_minutes,
            "sessionTokenHours": self.session_token_hours,
            "pythonExecutable": self.python_executable,
            "scriptsDir": self.scripts_dir,
            "aiChatTimeoutMinutes": self.ai_chat_timeout_minutes,
            "pcHangTimeoutMinutes": self.pc_hang_timeout_minutes,
            "pcHangSeconds": self.pc_hang_seconds,
            "bootWaitRounds": self.boot_wait_rounds,
            "bootWaitSecondsPerRound": self.boot_wait_seconds_per_round,
            "minHealthySessionSeconds": self.min_healthy_session_seconds,
            "browserMutexMode": self.browser_mutex_mode,
            "pollIntervalSeconds": self.poll_interval_seconds,
            "logHistorySize": self.log_history_size,
            "feishuWebhook": self.feishu_webhook,
            "feishuSecret": self.feishu_secret,
            "feishuAppId": self.feishu_app_id,
            "feishuAppSecret": self.feishu_app_secret,
            "feishuChatId": self.feishu_chat_id,
        }

    @classmethod
    def from_dict(cls, d):
        cfg = cls()
        cfg.accounts = [AccountConfig.from_dict(a) for a in (d.get("accounts") or [])]
        cfg.keep_alive_seconds = int(d.get("keepAliveSeconds", 60))
        cfg.admin_password = d.get("adminPassword", "admin")
        cfg.session_restart_minutes = int(d.get("sessionRestartMinutes", 1440))
        cfg.session_token_hours = int(d.get("sessionTokenHours", 12))
        cfg.python_executable = d.get("pythonExecutable", "") or ""
        cfg.scripts_dir = d.get("scriptsDir", "") or ""
        cfg.ai_chat_timeout_minutes = int(d.get("aiChatTimeoutMinutes", 15))
        cfg.pc_hang_timeout_minutes = int(d.get("pcHangTimeoutMinutes", 100))
        cfg.pc_hang_seconds = int(d.get("pcHangSeconds", 4800))
        cfg.boot_wait_rounds = int(d.get("bootWaitRounds", 3))
        cfg.boot_wait_seconds_per_round = int(d.get("bootWaitSecondsPerRound", 60))
        cfg.min_healthy_session_seconds = int(d.get("minHealthySessionSeconds", 60))
        cfg.browser_mutex_mode = d.get("browserMutexMode", "Global") or "Global"
        cfg.poll_interval_seconds = int(d.get("pollIntervalSeconds", 5))
        cfg.log_history_size = int(d.get("logHistorySize", 500))
        cfg.feishu_webhook = d.get("feishuWebhook", "") or ""
        cfg.feishu_secret = d.get("feishuSecret", "") or ""
        cfg.feishu_app_id = d.get("feishuAppId", "") or ""
        cfg.feishu_app_secret = d.get("feishuAppSecret", "") or ""
        cfg.feishu_chat_id = d.get("feishuChatId", "") or ""
        return cfg


class AccountConfig:
    def __init__(self):
        self.name = ""
        self.user = ""
        self.password = ""
        self.device_code = ""

    def to_dict(self):
        return {
            "name": self.name,
            "user": self.user,
            "password": self.password,
            "deviceCode": self.device_code,
        }

    @classmethod
    def from_dict(cls, d):
        a = cls()
        a.name = d.get("name", "") or ""
        a.user = d.get("user", "") or ""
        a.password = d.get("password", "") or ""
        a.device_code = d.get("deviceCode", "") or ""
        return a


class AccountStatusInfo:
    """账号运行时状态（key = 手机号）。desktops 为 dict 列表：name/code/desktopId/status。"""

    def __init__(self):
        self.is_running = False
        self.status_text = "未运行"
        self.desktops = []
        self.metrics = {
            "startedAt": 0,
            "uptimeSeconds": 0,
            "heartbeatSuccess": 0,
            "heartbeatFailed": 0,
            "consecutiveFailures": 0,
            "reconnectCount": 0,
            "lastHeartbeatAt": 0,
            "retryCount": 0,
            "nextRetryAt": 0,
            "nextRestartAt": 0,
            "lastError": "",
        }

    def to_dict(self):
        return {
            "isRunning": self.is_running,
            "statusText": self.status_text,
            "desktops": self.desktops,
            "metrics": self.metrics,
        }


class G:
    """全局单例（对应 C# GlobalState）。"""

    config = None  # AppConfig，server.main 中初始化
    global_stop = threading.Event()

    active_workers = {}      # key(手机号) -> KeepAliveSession（keepalive 模块定义）
    account_statuses = {}    # key(手机号) -> AccountStatusInfo
    pending_logins = {}      # user -> CtYunApi（等待验证码）
    pending_configs = {}     # user -> AccountConfig

    last_env_check = None    # 环境自检结果 dict
    channel_a_state = "Unknown"  # Unknown | Ok | LoginExpired
    points_cache = {}        # 手机号 -> {points, detail, checkedAt, error}（积分查询缓存）

    _gate = threading.RLock()

    @classmethod
    def statuses_get_or_add(cls, key):
        with cls._gate:
            s = cls.account_statuses.get(key)
            if s is None:
                s = AccountStatusInfo()
                cls.account_statuses[key] = s
            return s

    @classmethod
    def statuses_pop(cls, key):
        with cls._gate:
            cls.account_statuses.pop(key, None)


class ConfigStore:
    """配置读写：原子写 + .bak 备份 + 容错读（对应 C# ConfigStore.cs）。"""

    @staticmethod
    def load(path: str, from_dict, fallback):
        """文件不存在或解析失败时依次回落：.bak 备份 → fallback()。"""
        for target in (path, Paths.backup(path)):
            try:
                if not os.path.exists(target):
                    continue
                with open(target, "r", encoding="utf-8-sig") as f:
                    raw = f.read()
                if not raw.strip():
                    continue
                obj = from_dict(json.loads(raw))
                if target == Paths.backup(path):
                    logs.warn("配置", "已从备份文件恢复：" + target)
                return obj
            except Exception as ex:
                if target == path:
                    logs.fail("配置", "读取 %s 失败：%s。将尝试 .bak 备份。" % (path, ex))
                else:
                    logs.fail("配置", "从备份恢复 %s 失败：%s" % (Paths.backup(path), ex))
        return fallback()

    @staticmethod
    def save(path: str, to_dict) -> None:
        """先序列化到内存 → 备份原文件 → 写 .tmp → 原子替换。"""
        tmp = Paths.temp(path)
        try:
            data = json.dumps(to_dict(), ensure_ascii=False, indent=2)
            if os.path.exists(path):
                try:
                    with open(path, "rb") as src, open(Paths.backup(path), "wb") as dst:
                        dst.write(src.read())
                except Exception as ex:
                    logs.warn("配置", "备份 %s 失败：%s" % (path, ex))
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as ex:
            logs.fail("配置", "保存 %s 失败：%s（原文件未被破坏）" % (path, ex))
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise
