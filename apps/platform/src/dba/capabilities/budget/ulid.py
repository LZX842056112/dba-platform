"""ULID 生成器（预算预留 ID）。

★ 为什么用 ULID 而不是 uuid4：``budget_reservation.reservation_id`` 是 ``CHAR(26)``，
  ULID 恰好 26 字符（Crockford Base32），且**按时间单调递增**——同一天的预留在主键上
  天然有序，写放大更小、范围扫描更快。这也是 §5.2.4 把列宽定为 26 的原因。
"""

from __future__ import annotations

import os
import time

__all__ = ["new_reservation_id", "encode_ulid"]

#: Crockford Base32（去掉易混淆的 I/L/O/U）
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def encode_ulid(timestamp_ms: int, randomness: bytes) -> str:
    """把 48 位时间戳 + 80 位随机数编码为 26 字符 ULID。"""
    time_part = _encode(timestamp_ms & ((1 << 48) - 1), 10)
    rand_int = int.from_bytes(randomness[:10], "big")
    rand_part = _encode(rand_int, 16)
    return time_part + rand_part


def new_reservation_id() -> str:
    """生成一个新的 26 字符 ULID（用于 ``budget_reservation`` 主键）。"""
    return encode_ulid(int(time.time() * 1000), os.urandom(10))
