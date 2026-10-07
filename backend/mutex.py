# -*- coding: utf-8 -*-
"""
浏览器任务互斥锁（对应 C# BrowserMutex.cs）。

三种模式（配置 browserMutexMode）：
- "Global"：全局一把锁，所有浏览器任务互斥（并发度 1）
- "PerType"：按任务类型各一把锁，ai_chat 与 pc_hang 可并行（并发度 2）
- "PerAccount"：**按账号各一把锁，不同账号完全并行，同账号串行**
  （默认。4 个账号 → 并发度 4，吞吐约 4 倍；同账号串行是为了避免
   同一账号开两个浏览器互相顶掉登录态/保活会话）

关于并发安全：每个任务启动的是**独立的 Chromium 实例**（DrissionPage
ChromiumOptions + ChromiumPage，headless），彼此不共享 profile 与登录态文件，
因此跨账号并行在技术上是安全的；限制并发的真正原因是内存/CPU 与平台风控。

拒绝策略：本模块只提供 try/release（立即返回，不阻塞）；排队等待由
jobs.py 的 MUTEX_QUEUE_WAIT_SECONDS 循环实现。
"""
import threading

import logs
import store

MODE_GLOBAL = "Global"
MODE_PER_TYPE = "PerType"
MODE_PER_ACCOUNT = "PerAccount"


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
    _per_type = {}          # key=任务类型
    _per_account = {}       # key=账号（手机号）
    _gate = threading.Lock()
    current_holder = ""     # 兼容字段：最近一次成功获取的任务名
    _holders = {}           # key -> 任务名（PerType/PerAccount 下用于精确提示）

    # ---------- 键的解析 ----------

    @classmethod
    def _mode(cls) -> str:
        cfg = getattr(store.G, "config", None)
        mode = getattr(cfg, "browser_mutex_mode", MODE_GLOBAL) if cfg else MODE_GLOBAL
        return mode or MODE_GLOBAL

    @classmethod
    def _key_for(cls, job_type: str = None, account: str = None) -> str:
        """按当前模式算出锁的键。

        PerAccount：键 = 账号（不同账号并行，同账号串行）。账号为空时回退到
        任务类型，再为空则落到全局——避免所有「无账号任务」挤在同一把无名锁上
        或各拿各的锁（后者等于完全不互斥）。
        """
        mode = cls._mode()
        if mode == MODE_PER_ACCOUNT:
            return account or job_type or ""
        if mode == MODE_PER_TYPE:
            return job_type or ""
        return ""   # Global：键无意义

    @classmethod
    def _mutex_for(cls, table: dict, key: str, create: bool = True) -> _Mutex:
        """取键对应的互斥体。create=False 时仅在已存在才返回（查询不产生副作用）。"""
        with cls._gate:
            m = table.get(key)
            if m is None:
                if not create:
                    return None
                m = _Mutex()
                table[key] = m
            return m

    @classmethod
    def _resolve(cls, key: str, create: bool = True) -> _Mutex:
        """由「键」取到互斥体。调用方传入的 key 必须是 _key_for() 的结果。"""
        mode = cls._mode()
        if mode == MODE_PER_ACCOUNT:
            return cls._mutex_for(cls._per_account, key, create=create)
        if mode == MODE_PER_TYPE:
            return cls._mutex_for(cls._per_type, key, create=create)
        return cls._global_mutex

    # ---------- 获取 / 释放 ----------

    @classmethod
    def try_acquire(cls, job_type: str, job_name: str, token: str = None,
                    account: str = None) -> bool:
        key = cls._key_for(job_type, account)
        if cls._resolve(key).try_acquire(token):
            cls.current_holder = job_name or ""
            cls._holders[key] = job_name or ""
            return True
        return False

    @classmethod
    def release(cls, job_type: str, token: str = None, force: bool = False,
                account: str = None) -> bool:
        key = cls._key_for(job_type, account)
        ok = cls._resolve(key).release(token=token, force=force)
        if ok:
            cls._holders.pop(key, None)
            if cls.current_holder == cls._holders.get(key, ""):
                cls.current_holder = ""
        return ok

    @classmethod
    def is_held(cls, job_type: str, account: str = None) -> bool:
        # create=False：查询状态不该凭空创建锁，否则 all_keys() 会混入
        # 从未真正持有的账号键（曾让自愈逻辑去检查一堆空锁）。
        m = cls._resolve(cls._key_for(job_type, account), create=False)
        return bool(m and m.is_held())

    # ---------- 自愈支持 ----------

    @classmethod
    def all_keys(cls):
        """返回当前模式下需要检查泄漏的全部键（供 jobs.reap_stale_state 使用）。"""
        mode = cls._mode()
        if mode == MODE_PER_ACCOUNT:
            with cls._gate:
                return list(cls._per_account.keys())
        if mode == MODE_PER_TYPE:
            with cls._gate:
                return list(cls._per_type.keys())
        return [""]     # Global

    @classmethod
    def release_key(cls, key: str, force: bool = True) -> bool:
        """按原始键强制释放（自愈回收泄漏锁用，绕过 _key_for 重算）。"""
        ok = cls._resolve(key).release(force=force)
        if ok:
            cls._holders.pop(key, None)
        return ok


def mutex_message(job_type: str = None, account: str = None) -> str:
    """生成「被互斥跳过」的提示文案（按当前模式给出准确的占用者）。"""
    mode = BrowserMutex._mode()
    key = BrowserMutex._key_for(job_type, account)
    holder = BrowserMutex._holders.get(key) or BrowserMutex.current_holder or "未知任务"
    if mode == MODE_PER_ACCOUNT:
        return ("浏览器任务互斥：账号 %s 已有任务「%s」在运行，同一账号同一时刻"
                "只允许一个浏览器实例（不同账号可并行）。请稍候自动排队，"
                "或在设置中心切换互斥模式。" % (account or "-", holder))
    if mode == MODE_PER_TYPE:
        return ("浏览器任务互斥：同类型任务「%s」正在运行（当前模式：按类型互斥）。"
                "请先停止该任务，或在设置中心切换为「按账号互斥」以提升并发。"
                % holder)
    return ("浏览器任务互斥：" + holder + " 正在运行，同一时刻只允许一个浏览器实例。"
            "请先停止该任务，或在设置中心切换为「按账号互斥」（推荐，不同账号可并行）。")
