# -*- coding: utf-8 -*-
"""
浏览器任务互斥锁（对应 C# BrowserMutex.cs）。
- "Global"（默认）：ai_chat 与 pc_hang 互斥，全局一把锁
- "PerType"：按任务类型各一把锁，不同类型可并行
拒绝策略：立即拒绝、不排队；CurrentHolder 用于拒绝提示文案。
"""
import threading

import logs
import store


class _Mutex:
    """单个非重入互斥体：try_acquire / release / is_held。

    release 支持「持有者令牌」：只有传入与获取时相同的 token 才释放。
    这样可防止经典竞态——任务 A 被「停止」强制释放锁后，锁立刻被任务 B 拿走，
    此时 A 的 finally 再执行 release 会把 B 的锁误放，导致两个浏览器任务并行。
    force=True 表示无条件释放（停止任务 / 自愈回收泄漏锁场景）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False
        self.token = None

    def try_acquire(self, token=None) -> bool:
        if self._lock.acquire(blocking=False):
            self.held = True
            self.token = token
            return True
        return False

    def release(self, token=None, force=False) -> bool:
        # 提供了 token 且当前持有者不同 → 拒绝释放（不误放他人的锁）
        if (not force) and token is not None and self.token is not None and token != self.token:
            return False
        try:
            self._lock.release()
        except (ValueError, RuntimeError):
            pass  # 重复释放等情况忽略
        self.held = False
        self.token = None
        return True

    def is_held(self) -> bool:
        return self.held


class BrowserMutex:
    _global_mutex = _Mutex()
    _per_type = {}
    _per_type_gate = threading.Lock()
    current_holder = ""

    @classmethod
    def _resolve(cls, job_type: str) -> _Mutex:
        mode = store.G.config.browser_mutex_mode if store.G.config else "Global"
        if mode == "PerType":
            key = job_type or ""
            with cls._per_type_gate:
                m = cls._per_type.get(key)
                if m is None:
                    m = _Mutex()
                    cls._per_type[key] = m
                return m
        return cls._global_mutex

    @classmethod
    def try_acquire(cls, job_type: str, job_name: str, token: str = None) -> bool:
        m = cls._resolve(job_type)
        if m.try_acquire(token):
            cls.current_holder = job_name or ""
            return True
        return False

    @classmethod
    def release(cls, job_type: str, token: str = None, force: bool = False) -> bool:
        m = cls._resolve(job_type)
        ok = m.release(token=token, force=force)
        if ok:
            cls.current_holder = ""
        return ok

    @classmethod
    def is_held(cls, job_type: str) -> bool:
        return cls._resolve(job_type).is_held()


def mutex_message() -> str:
    return ("浏览器任务互斥：" + BrowserMutex.current_holder +
            " 正在运行，同一时刻只允许一个浏览器实例。请先停止该任务，或在设置中心切换为「按类型互斥」。")
