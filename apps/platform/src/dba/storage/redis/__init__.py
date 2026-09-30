"""Redis 存储适配层（L5）。

对齐《设计文档 v2》§5.6 / §6.5 / §6.13 与《实现要点清单》§1.2.6。

本包提供三类能力，全部以 **Lua 原子脚本**为核心：
* 预算预留/结算/释放（``budget_reservation`` 的**快路径**，权威账本在 MySQL）；
* 运行进度 ``run:progress``（§5.6，保留最近 200 条、TTL 1h）；
* 令牌桶限流与幂等键（``SET NX``）。
"""

from __future__ import annotations

from .client import RedisStorage, build_redis
from .repo import RedisBudgetCache, RedisIdempotency, RedisProgressWriter, RedisRateLimiter
from .scripts import SCRIPTS

__all__ = [
    "SCRIPTS",
    "RedisBudgetCache",
    "RedisIdempotency",
    "RedisProgressWriter",
    "RedisRateLimiter",
    "RedisStorage",
    "build_redis",
]
