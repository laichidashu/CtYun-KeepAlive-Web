# -*- coding: utf-8 -*-
"""
Web 管理会话令牌（对应 C# AdminSessionStore.cs + RequestAuth）。
- 多会话令牌表，带过期，可吊销
- 上限 32 个会话，超出淘汰最早发出的令牌
- 令牌 = 32 随机字节的十六进制（64 字符）
"""
import threading
import time
import secrets

_MAX_SESSIONS = 32
_TOKEN_BYTES = 32
_DEFAULT_HOURS = 12
_MAX_HOURS = 24 * 30


class AdminSessionStore:
    _gate = threading.Lock()
    _sessions = {}   # token -> expires_at (Unix 秒)
    _order = []      # 按签发顺序的 token 列表，用于淘汰最早会话

    @classmethod
    def issue_with_expiry(cls, hours: int):
        ttl = hours if hours and hours > 0 else _DEFAULT_HOURS
        if ttl > _MAX_HOURS:
            ttl = _MAX_HOURS
        now = int(time.time())
        expires_at = now + ttl * 3600
        token = secrets.token_hex(_TOKEN_BYTES).upper()

        with cls._gate:
            cls._purge_expired_locked(now)
            while len(cls._sessions) >= _MAX_SESSIONS and cls._order:
                oldest = cls._order.pop(0)
                cls._sessions.pop(oldest, None)
            cls._sessions[token] = expires_at
            cls._order.append(token)
        return token, expires_at

    @classmethod
    def validate(cls, token: str) -> bool:
        if not token or not token.strip():
            return False
        now = int(time.time())
        with cls._gate:
            cls._purge_expired_locked(now)
            expires_at = cls._sessions.get(token)
            return expires_at is not None and expires_at > now

    @classmethod
    def revoke(cls, token: str):
        if not token or not token.strip():
            return
        with cls._gate:
            if cls._sessions.pop(token, None) is not None:
                try:
                    cls._order.remove(token)
                except ValueError:
                    pass

    @classmethod
    def revoke_all(cls):
        with cls._gate:
            cls._sessions.clear()
            cls._order.clear()

    @classmethod
    def _purge_expired_locked(cls, now: int):
        expired = [t for t, exp in cls._sessions.items() if exp <= now]
        for t in expired:
            cls._sessions.pop(t, None)
            try:
                cls._order.remove(t)
            except ValueError:
                pass


def get_token_from_headers(headers, query) -> str:
    """提取令牌：X-Auth-Token 请求头 → token 查询参数（SSE 用）。"""
    header = headers.get("X-Auth-Token", "")
    if header and header.strip():
        return header.strip()
    return (query.get("token", "") or "").strip()
