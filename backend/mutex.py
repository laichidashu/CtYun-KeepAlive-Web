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
    """单个非重入互斥体：try_acquire / release / is_held。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False

    def try_acquire(self) -> bool:
        if self._lock.acquire(blocking=False):
            self.held = True
            return True
        return False

    def release(self):
        try:
            self._lock.release()
        except (ValueError, RuntimeError):
            pass  # 重复释放等情况忽略
        self.held = False

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
    def try_acquire(cls, job_type: str, job_name: str) -> bool:
        m = cls._resolve(job_type)
        if m.try_acquire():
            cls.current_holder = job_name or ""
            return True
        return False

    @classmethod
    def release(cls, job_type: str):
        m = cls._resolve(job_type)
        m.release()
        cls.current_holder = ""

    @classmethod
    def is_held(cls, job_type: str) -> bool:
        return cls._resolve(job_type).is_held()


def mutex_message() -> str:
    return ("浏览器任务互斥：" + BrowserMutex.current_holder +
            " 正在运行，同一时刻只允许一个浏览器实例。请先停止该任务，或在设置中心切换为「按类型互斥」。")
