# -*- coding: utf-8 -*-
"""图形验证码的一次性存储（进程内内存字典）

设计要点：
  * **一次性**：``consume()`` 无论校验成功还是失败都会把 key 删掉。
    如果只在成功时删，攻击者就能拿同一张图反复试（4 位数字只有 1 万种组合），
    验证码等于形同虚设。所以一次输入即作废，错了让用户换一张。
  * **有 TTL**：默认 5 分钟，过期的 key 既取不到也占不到内存（issue 时顺带清理）。
  * **容量上限**：异常流量下字典不会无限膨胀，超过 ``MAX_ENTRIES`` 先清过期项，
    仍然超限就丢掉最早的一批（验证码丢了最多让用户刷新一次）。
  * **进程内**：后端以 ``--workers 1`` 运行，单进程字典足够；
    若将来扩多进程/多实例，需要换成 Redis 之类的共享存储，否则会跨进程对不上。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, Optional, Tuple

import config


class CaptchaStore:
    """``key -> (验证码文本, 过期时间戳)`` 的线程安全字典。"""

    def __init__(self, ttl_seconds: Optional[int] = None, max_entries: int = 5000) -> None:
        self._ttl = int(ttl_seconds if ttl_seconds is not None else config.CAPTCHA_TTL_SECONDS)
        self._max = int(max_entries)
        self._items: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    # -- 写入 --------------------------------------------------------------
    def issue(self, code: str) -> str:
        """登记一个验证码，返回唯一的 ``captcha_key``（UUID4）。"""
        key = str(uuid.uuid4())
        expire_at = time.time() + self._ttl
        with self._lock:
            self._purge_locked()
            self._items[key] = (code, expire_at)
        return key

    # -- 校验 --------------------------------------------------------------
    def consume(self, key: str, code: str) -> bool:
        """校验并**立即删除**该 key；过期、不存在、不匹配都返回 False。

        key 存在但验证码错误时同样删除：一张图只能试一次。
        """
        if not key or not code:
            return False

        with self._lock:
            item = self._items.pop(key, None)   # 无论结果如何都不再可用

        if item is None:
            return False

        expected, expire_at = item
        if time.time() > expire_at:
            return False
        # 用户输入可能带空格，且印刷体数字不易区分大小写
        return code.strip().lower() == expected.strip().lower()

    # -- 只读（测试/排查用，不改变状态）---------------------------------------
    def peek(self, key: str) -> Optional[str]:
        """返回验证码文本但不删除，仅供单元测试读取。"""
        with self._lock:
            item = self._items.get(key)
        if item is None:
            return None
        code, expire_at = item
        return None if time.time() > expire_at else code

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    # -- 内部 --------------------------------------------------------------
    def _purge_locked(self) -> None:
        """清理过期项；仍超过容量上限时按插入顺序淘汰最早的一批。"""
        now = time.time()
        expired = [k for k, (_, exp) in self._items.items() if exp < now]
        for k in expired:
            del self._items[k]

        overflow = len(self._items) - self._max + 1
        if overflow > 0:
            for k in list(self._items.keys())[:overflow]:
                del self._items[k]


# 全局单例：所有请求共享同一份缓存
captcha_store = CaptchaStore()