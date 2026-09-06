# -*- coding: utf-8 -*-
"""
日志系统（对应 C# Utility.cs + LogBroadcaster.cs）。
- 全局环形历史缓冲（默认 500 条，可由 logHistorySize 覆盖）
- 每个订阅者一条独立有界队列（满则丢最旧），慢消费者不拖垮生产者
- 日志行格式：[HH:mm:ss.ff] [来源] 消息
"""
import os
import queue
import sys
import threading
import time
from datetime import datetime

# 日志级别：数值越小越轻微，前端按此着色
LEVEL_INFO = 0
LEVEL_SUCCESS = 1
LEVEL_WARN = 2
LEVEL_ERROR = 3

_DEFAULT_HISTORY_SIZE = 500
_SUB_QUEUE_SIZE = 256

# ---- 本地文件持久化 ----
# <数据目录>/logs/app-YYYY-MM-DD.log，按天滚动，默认保留 30 天
_LEVEL_TAGS = {LEVEL_INFO: "INFO", LEVEL_SUCCESS: "OK", LEVEL_WARN: "WARN", LEVEL_ERROR: "ERROR"}
_FILE_GATE = threading.Lock()
_FILE_DIR = ""
_FILE_DATE = ""
_FILE_HANDLE = None
_RETENTION_DAYS = 30


def format_line(source: str, message: str) -> str:
    src = source if source and source.strip() else "系统"
    now = datetime.now()
    return "[%s.%02d] [%s] %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), now.microsecond // 10000, src, message or "")


def mask_user(user: str) -> str:
    """手机号掩码：长度 ≥7 → 前 3 + **** + 后 4；否则 ***。"""
    if not user or not user.strip():
        return "***"
    u = user.strip()
    if len(u) < 7:
        return "***"
    return u[:3] + "****" + u[-4:]


class _Subscriber:
    def __init__(self):
        self.q = queue.Queue(maxsize=_SUB_QUEUE_SIZE)

    def put_drop_oldest(self, entry):
        while True:
            try:
                self.q.put_nowait(entry)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass


class LogBroadcaster:
    def __init__(self):
        self._gate = threading.Lock()
        self._history = []          # 环形历史（list，尾部最新）
        self._subs = {}             # id -> _Subscriber
        self._next_id = 0
        self._history_limit = _DEFAULT_HISTORY_SIZE

    @property
    def subscriber_count(self):
        with self._gate:
            return len(self._subs)

    def set_history_limit(self, size: int):
        with self._gate:
            self._history_limit = max(0, int(size))
            while len(self._history) > self._history_limit:
                self._history.pop(0)

    def publish(self, level: int, source: str, message: str):
        src = source if source and source.strip() else "系统"
        entry = {
            "ts": int(time.time() * 1000),
            "level": level,
            "source": src,
            "line": format_line(src, message),
        }
        with self._gate:
            if self._history_limit > 0:
                self._history.append(entry)
                while len(self._history) > self._history_limit:
                    self._history.pop(0)
            snapshot = list(self._subs.values())
        # 锁外投递：只 put_nowait，永不阻塞
        for sub in snapshot:
            sub.put_drop_oldest(entry)

    def subscribe(self):
        """返回 (订阅 Id, 队列, 历史快照)。断开时必须 unsubscribe 防泄漏。"""
        with self._gate:
            self._next_id += 1
            sid = self._next_id
            sub = _Subscriber()
            self._subs[sid] = sub
            return sid, sub, list(self._history)

    def unsubscribe(self, sid):
        with self._gate:
            self._subs.pop(sid, None)

    def snapshot(self):
        with self._gate:
            return list(self._history)


Log = LogBroadcaster()


def _write_line(source: str, message: str):
    try:
        sys.stdout.write(format_line(source, message) + "\n")
        sys.stdout.flush()
    except Exception:
        pass  # 无控制台环境（容器后台）下忽略输出异常，日志仍需广播


def set_file_dir(d: str):
    """启用本地文件日志，指定日志目录（server 启动时 Paths.initialize 后调用一次）。"""
    global _FILE_DIR
    if not (d and d.strip()):
        return
    try:
        os.makedirs(d, exist_ok=True)
        _FILE_DIR = d
    except Exception:
        pass  # 目录创建失败则降级为仅内存+控制台


def _cleanup_old_logs():
    """删除超过保留期的历史日志文件。调用方已持有 _FILE_GATE。"""
    try:
        cutoff = time.time() - _RETENTION_DAYS * 86400
        for name in os.listdir(_FILE_DIR):
            if name.startswith("app-") and name.endswith(".log"):
                p = os.path.join(_FILE_DIR, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
    except Exception:
        pass


def _write_file_line(level: int, source: str, message: str):
    if not _FILE_DIR:
        return
    global _FILE_DATE, _FILE_HANDLE
    day = datetime.now().strftime("%Y-%m-%d")
    tag = _LEVEL_TAGS.get(level, "INFO")
    with _FILE_GATE:
        try:
            if _FILE_HANDLE is None or _FILE_DATE != day:
                try:
                    if _FILE_HANDLE:
                        _FILE_HANDLE.close()
                except Exception:
                    pass
                _FILE_DATE = day
                _FILE_HANDLE = open(os.path.join(_FILE_DIR, "app-%s.log" % day), "a", encoding="utf-8")
                _cleanup_old_logs()
            _FILE_HANDLE.write("[%s] %s\n" % (tag, format_line(source, message)))
            _FILE_HANDLE.flush()
        except Exception:
            pass  # 文件写入失败不影响内存广播与控制台输出


def write_line(message, level=LEVEL_INFO, source="系统"):
    src = source if source and source.strip() else "系统"
    _write_line(src, message)
    Log.publish(level, src, message)
    _write_file_line(level, src, message)


def info(source: str, message: str):
    write_line("[" + source + "] " + message, LEVEL_INFO, source)


def ok(source: str, message: str):
    write_line("[" + source + "] " + message, LEVEL_SUCCESS, source)


def warn(source: str, message: str):
    write_line("[" + source + "] " + message, LEVEL_WARN, source)


def fail(source: str, message: str):
    write_line("[" + source + "] " + message, LEVEL_ERROR, source)
    # 飞书错误推送（未配置时内部静默跳过；任何异常不影响日志主流程）
    try:
        import feishu
        feishu.notify_error(source, message)
    except Exception:
        pass
