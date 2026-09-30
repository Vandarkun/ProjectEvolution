"""统一 ID 生成器：可读前缀 + ULID。

约定（见 docs/01-architecture/02-data-model.md §2）：

- 前缀 + ULID，例如 ``TASK-01ARZ3NDEKTSV4RRFFQ69G5FAV``；
- 全局唯一：48bit 毫秒时间戳 + 80bit 加密随机数；
- 不依赖数据库自增；
- ID 一旦创建不可改变，且不承载业务排序含义（Canonical 排序使用 Ledger 的 seq）。

同进程内同一毫秒发生碰撞时随机部分单调 +1，进一步降低并发碰撞概率。
"""

from __future__ import annotations

import os
import threading
import time

from project_evolution.errors import ValidationError

#: 允许的 ID 前缀（02-data-model.md §2 的固定清单 + 04-event-ledger.md §6 的 ANL，
#: 不再自行扩展）
PREFIXES = frozenset({"PRJ", "SES", "TASK", "CHK", "RAW", "EVO", "EVD", "MS", "VER", "ANL"})

# Crockford Base32：去掉易混淆的 I L O U
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LEN = 26
_RAND_BITS = 80

_last_ts_ms = 0
_last_rand = 0
_lock = threading.Lock()


def new_id(prefix: str) -> str:
    """生成形如 ``PREFIX-<ULID>`` 的全局唯一 ID。

    :raises ValidationError: prefix 不在 :data:`PREFIXES` 中。
    """
    global _last_ts_ms, _last_rand
    if prefix not in PREFIXES:
        raise ValidationError(
            f"unknown ID prefix {prefix!r}, allowed prefixes: {sorted(PREFIXES)}"
        )
    ts_ms = time.time_ns() // 1_000_000
    with _lock:
        rand = int.from_bytes(os.urandom(10), "big")
        if ts_ms == _last_ts_ms and rand <= _last_rand:
            rand = _last_rand + 1
            if rand >= 1 << _RAND_BITS:  # pragma: no cover - 80bit 溢出不可能在实践中发生
                rand = 0
        _last_ts_ms, _last_rand = ts_ms, rand
    return f"{prefix}-{_encode(ts_ms, rand)}"


def _encode(ts_ms: int, rand: int) -> str:
    """128bit (48bit 时间 + 80bit 随机) -> 26 位 Crockford Base32。"""
    value = (ts_ms << _RAND_BITS) | rand
    chars = []
    for _ in range(_ULID_LEN):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
