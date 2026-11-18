# -*- coding: utf-8 -*-
"""登录防爆破：连续密码错误计数 + 锁定

规则（可用环境变量调整，见 config.py）：
  * 同一**用户名**或同一**IP** 连续输错密码 5 次 → 锁定 15 分钟；
  * 锁定期内登录一律返回 429「请稍后再试」，不再校验密码（也就不用付 bcrypt 的计算成本）；
  * 登录成功立刻清零该用户名与该 IP 的计数；
  * 计数按「连续」理解：锁定到期后重新从 0 开始，不会一次输错就永久累加。

实现取舍：
  * 进程内字典，够单进程 uvicorn（--workers 1）用；
    扩成多进程/多实例时必须换成 Redis 之类共享存储，否则每个进程各锁各的。
  * 键分两套：``u:<username>`` 防定向撞库，``ip:<ip>`` 防同一来源轮换用户名爆破。
    两者任一触发即锁定，因此「换用户名继续打同一 IP」同样会被挡住。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

import config


class LoginGuard:
    """线程安全的失败计数 + 锁定表。"""

    def __init__(
        self,
        max_failures: Optional[int] = None,
        lock_seconds: Optional[int] = None,
    ) -> None:
        self._max = int(max_failures if max_failures is not None else config.LOGIN_MAX_FAILURES)
        self._lock_seconds = int(
            lock_seconds if lock_seconds is not None else config.LOGIN_LOCK_SECONDS
        )
        self._fails: Dict[str, Tuple[int, float]] = {}   # key -> (连续失败次数, 最近一次时间)
        self._locked_until: Dict[str, float] = {}        # key -> 解锁时间戳
        self._lock = threading.Lock()

    # -- 查询 --------------------------------------------------------------
    def locked_remaining(self, username: str, ip: str = "") -> int:
        """还剩多少秒解锁；未锁定返回 0。"""
        now = time.time()
        remaining = 0
        with self._lock:
            for key in self._keys(username, ip):
                until = self._locked_until.get(key, 0)
                if until > now:
                    remaining = max(remaining, int(until - now) + 1)
        return remaining

    # -- 写入 --------------------------------------------------------------
    def record_failure(self, username: str, ip: str = "") -> int:
        """记一次密码错误；若因此进入锁定，返回剩余锁定秒数，否则 0。"""
        now = time.time()
        locked_for = 0
        with self._lock:
            self._gc_locked(now)
            for key in self._keys(username, ip):
                if self._locked_until.get(key, 0) > now:
                    continue          # 已经在锁定期，不重复计数
                count, _ = self._fails.get(key, (0, now))
                count += 1
                self._fails[key] = (count, now)
                if count >= self._max:
                    self._locked_until[key] = now + self._lock_seconds
                    self._fails[key] = (0, now)     # 解锁后从 0 重新计
                    locked_for = max(locked_for, self._lock_seconds)
        return locked_for

    def reset(self, username: str, ip: str = "") -> None:
        """登录成功后清零：之前的失败不再累计。"""
        with self._lock:
            for key in self._keys(username, ip):
                self._fails.pop(key, None)
                self._locked_until.pop(key, None)

    def clear(self) -> None:
        """全部清空（测试用）。"""
        with self._lock:
            self._fails.clear()
            self._locked_until.clear()

    # -- 内部 --------------------------------------------------------------
    @staticmethod
    def _keys(username: str, ip: str) -> Tuple[str, ...]:
        keys = []
        if username:
            keys.append("u:" + username)
        if ip:
            keys.append("ip:" + ip)
        return tuple(keys)

    def _gc_locked(self, now: float) -> None:
        """清理已解锁且早已过期的计数，避免字典随攻击流量无限增长。"""
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]
        stale = [
            k
            for k, (_, last) in self._fails.items()
            if now - last > self._lock_seconds * 4
        ]
        for key in stale:
            del self._fails[key]


# 全局单例
login_guard = LoginGuard()