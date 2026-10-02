"""Redis Repo：预算快路径缓存 / 进度流 / 限流 / 幂等键。

对齐《设计文档 v2》§5.6 / §6.13 与《实现要点清单》§1.2.6。

键空间约定（§5.6）：
* ``budget:usage:{budget_id}:{period_start}``   HASH{consumed, reserved}
* ``budget:rsv:{reservation_id}``               HASH{state, estimated, budget_id, period_start}
* ``run:progress:{trace_id}``                   LIST（LTRIM 200，TTL 1h）
* ``rl:{scope}``                                HASH{tokens, ts}（令牌桶）
* ``idem:{key}``                                STRING（SET NX EX）
"""

from __future__ import annotations

import time
from typing import Any

from .client import RedisStorage

__all__ = ["RedisBudgetCache", "RedisProgressWriter", "RedisRateLimiter", "RedisIdempotency"]

#: 预留快照 TTL：略大于单次 Run 墙钟预算，避免长期占用内存
_RSV_TTL_S = 3600


class RedisBudgetCache:
    """预算预留的**快路径**（权威账本仍是 MySQL ``budget_reservation``）。

    ★ P1-9：Redis 不可用时 **fail-open 到 MySQL 原子路径**（由 capabilities.budget 决定），
    本类只负责 Redis 侧原子操作与幂等返回码解析。
    """

    def __init__(self, storage: RedisStorage) -> None:
        self._storage = storage

    @staticmethod
    def usage_key(budget_id: int, period_start: str) -> str:
        return f"budget:usage:{budget_id}:{period_start}"

    @staticmethod
    def rsv_key(reservation_id: str) -> str:
        return f"budget:rsv:{reservation_id}"

    async def reserve(
        self,
        *,
        budget_id: int,
        period_start: str,
        reservation_id: str,
        amount: int,
        hard_limit: int,
    ) -> dict[str, Any]:
        """原子预留。返回 ``{code, used, granted}``；``code`` 见 ``scripts.py`` 说明。"""
        result = await self._storage.run_script(
            "budget_reserve",
            keys=[self.usage_key(budget_id, period_start), self.rsv_key(reservation_id)],
            args=[amount, hard_limit, reservation_id, _RSV_TTL_S, budget_id, period_start],
        )
        code = int(result[0])
        used = int(result[1])
        return {"code": code, "used": used, "granted": code == 1}

    async def settle(
        self, *, budget_id: int, period_start: str, reservation_id: str, actual: int
    ) -> dict[str, Any]:
        result = await self._storage.run_script(
            "budget_settle",
            keys=[self.usage_key(budget_id, period_start), self.rsv_key(reservation_id)],
            args=[actual],
        )
        code = int(result[0])
        return {
            "code": code,
            "estimated": int(result[1]),
            "status": {1: "settled", 2: "already_settled", 0: "reservation_missing"}[code],
        }

    async def release(
        self, *, budget_id: int, period_start: str, reservation_id: str
    ) -> dict[str, Any]:
        result = await self._storage.run_script(
            "budget_release",
            keys=[self.usage_key(budget_id, period_start), self.rsv_key(reservation_id)],
            args=[],
        )
        code = int(result[0])
        return {
            "code": code,
            "estimated": int(result[1]),
            "status": {1: "released", 2: "already_released", 0: "reservation_missing"}[code],
        }

    async def usage(self, budget_id: int, period_start: str) -> dict[str, int]:
        data = await self._storage.client.hgetall(self.usage_key(budget_id, period_start))
        return {"consumed": int(data.get("consumed", 0)), "reserved": int(data.get("reserved", 0))}

    async def seed(
        self, budget_id: int, period_start: str, *, consumed: int, reserved: int
    ) -> None:
        """预热快路径（从 MySQL 权威账本回填，避免缓存冷启动误判）。"""
        await self._storage.client.hset(
            self.usage_key(budget_id, period_start),
            mapping={"consumed": consumed, "reserved": reserved},
        )


class RedisProgressWriter:
    """``run:progress`` 进度流（§5.6）：保留最近 200 条、TTL 1h。

    为什么用 Redis 而不是进程内 list（v1 做法）：SSE 断线重连需从**服务端**回放；
    进程内 list 在多副本部署下读不到别的副本写的事件。
    """

    MAX = 200
    TTL_S = 3600
    META_TTL_S = 86400

    def __init__(self, storage: RedisStorage) -> None:
        self._storage = storage

    @staticmethod
    def _key(trace_id: str) -> str:
        return f"run:progress:{trace_id}"

    @staticmethod
    def _meta_key(trace_id: str) -> str:
        return f"run:progress:meta:{trace_id}"

    async def write(self, trace_id: str, envelope: dict[str, Any]) -> None:
        import json  # noqa: PLC0415

        key = self._key(trace_id)
        await self._storage.client.rpush(key, json.dumps(envelope, ensure_ascii=False))
        await self._storage.client.ltrim(key, -self.MAX, -1)
        await self._storage.client.expire(key, self.TTL_S)
        # 单独保留最新游标一天；历史列表过期后，重连端仍可得知存在缺口。
        meta_key = self._meta_key(trace_id)
        await self._storage.client.hset(
            meta_key,
            mapping={"latest_seq": int(envelope.get("seq", 0))},
        )
        await self._storage.client.expire(meta_key, self.META_TTL_S)

    async def replay(self, trace_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        import json  # noqa: PLC0415

        raw = await self._storage.client.lrange(self._key(trace_id), 0, -1)
        events = (json.loads(item) for item in raw)
        return [env for env in events if int(env.get("seq", 0)) > after_seq]

    async def replay_page(
        self, trace_id: str
    ) -> tuple[list[dict[str, Any]], int | None, int | None]:
        """读取保留事件与序号水位；水位 TTL 长于事件列表以检测完整过期。"""
        import json  # noqa: PLC0415

        raw = await self._storage.client.lrange(self._key(trace_id), 0, -1)
        events = [json.loads(item) for item in raw]
        oldest = min((int(event.get("seq", 0)) for event in events), default=None)
        latest = max((int(event.get("seq", 0)) for event in events), default=None)
        stored_latest = await self._storage.client.hget(self._meta_key(trace_id), "latest_seq")
        if stored_latest is not None:
            latest = max(latest or 0, int(stored_latest))
        return events, oldest, latest


class RedisRateLimiter:
    """分布式令牌桶（Lua 原子）。"""

    def __init__(self, storage: RedisStorage, *, rate: int = 100, burst: int = 200) -> None:
        self._storage = storage
        self._rate = rate
        self._burst = burst

    async def allow(self, key: str, *, cost: int = 1) -> bool:
        now_ms = int(time.time() * 1000)
        result = await self._storage.run_script(
            "token_bucket",
            keys=[f"rl:{key}"],
            args=[self._rate, self._burst, now_ms, cost],
        )
        return int(result[0]) == 1


class RedisIdempotency:
    """幂等键（``SET NX EX``）——用于 API 重试去重（§7.5）。"""

    def __init__(self, storage: RedisStorage) -> None:
        self._storage = storage

    async def claim(self, key: str, *, ttl_s: int = 300) -> bool:
        """抢占一个幂等键；返回 ``True`` 表示首次（可执行），``False`` 表示重复。"""
        return await self._storage.set_nx(f"idem:{key}", "1", ttl_s=ttl_s)

    async def seen(self, key: str) -> bool:
        return await self._storage.get(f"idem:{key}") is not None
