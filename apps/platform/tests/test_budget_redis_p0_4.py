"""★ DoD: P0-4 幂等 + 并发不超卖（Redis Lua 快路径，fakeredis 真实 Lua）。

不依赖 MySQL：直接驱动 ``RedisBudgetCache`` 的原子脚本，验证：
  * 200 并发预留**不超卖**（恰好 100 笔通过）；
  * 同 ``reservation_id`` 二次预留 → 幂等命中（code=2），不重复占用；
  * 二次结算 → ``already_settled``，reserved **只扣一次**；
  * 二次释放 → ``already_released``，reserved **只扣一次**。
"""

from __future__ import annotations

import asyncio

from dba.storage.redis.repo import RedisBudgetCache


async def test_concurrent_reserve_no_oversell(redis_storage: object) -> None:
    cache = RedisBudgetCache(redis_storage)  # type: ignore[arg-type]
    hard_limit = 100_000
    amount = 1_000

    async def one(i: int) -> int:
        res = await cache.reserve(
            budget_id=1,
            period_start="2026-09-01",
            reservation_id=f"RSV{i:026d}",
            amount=amount,
            hard_limit=hard_limit,
        )
        return int(res["code"])

    codes = await asyncio.gather(*(one(i) for i in range(200)))
    granted = sum(1 for c in codes if c == 1)
    denied = sum(1 for c in codes if c == 0)

    # 恰好 100 笔通过（100 * 1000 == hard_limit），绝不超过 → 不超卖
    assert granted == 100, f"超卖或漏放：granted={granted}"
    assert denied == 100
    usage = await cache.usage(1, "2026-09-01")
    assert usage["reserved"] == hard_limit
    assert usage["consumed"] == 0


async def test_reserve_is_idempotent(redis_storage: object) -> None:
    cache = RedisBudgetCache(redis_storage)  # type: ignore[arg-type]
    kwargs = dict(
        budget_id=2,
        period_start="2026-09-01",
        reservation_id="R" * 26,
        amount=5_000,
        hard_limit=100_000,
    )
    first = await cache.reserve(**kwargs)  # type: ignore[arg-type]
    second = await cache.reserve(**kwargs)  # type: ignore[arg-type]
    assert first["code"] == 1
    assert second["code"] == 2  # 幂等命中
    usage = await cache.usage(2, "2026-09-01")
    assert usage["reserved"] == 5_000  # 只占用一次


async def test_settle_and_release_are_idempotent(redis_storage: object) -> None:
    cache = RedisBudgetCache(redis_storage)  # type: ignore[arg-type]
    await cache.reserve(
        budget_id=3,
        period_start="2026-09-01",
        reservation_id="S" * 26,
        amount=8_000,
        hard_limit=100_000,
    )

    s1 = await cache.settle(
        budget_id=3, period_start="2026-09-01", reservation_id="S" * 26, actual=6_000
    )
    assert s1["status"] == "settled"
    after_first = await cache.usage(3, "2026-09-01")
    assert after_first == {"consumed": 6_000, "reserved": 0}

    # 二次结算：幂等，不得再改数
    s2 = await cache.settle(
        budget_id=3, period_start="2026-09-01", reservation_id="S" * 26, actual=9_999
    )
    assert s2["status"] == "already_settled"
    after_second = await cache.usage(3, "2026-09-01")
    assert after_second == {"consumed": 6_000, "reserved": 0}  # 未被 9999 覆盖

    # 释放路径
    await cache.reserve(
        budget_id=3,
        period_start="2026-09-01",
        reservation_id="T" * 26,
        amount=2_000,
        hard_limit=100_000,
    )
    r1 = await cache.release(budget_id=3, period_start="2026-09-01", reservation_id="T" * 26)
    assert r1["status"] == "released"
    r2 = await cache.release(budget_id=3, period_start="2026-09-01", reservation_id="T" * 26)
    assert r2["status"] == "already_released"
    usage = await cache.usage(3, "2026-09-01")
    assert usage == {"consumed": 6_000, "reserved": 0}


async def test_missing_reservation_is_reported(redis_storage: object) -> None:
    cache = RedisBudgetCache(redis_storage)  # type: ignore[arg-type]
    s = await cache.settle(
        budget_id=9, period_start="2026-09-01", reservation_id="X" * 26, actual=1
    )
    assert s["status"] == "reservation_missing"
    r = await cache.release(budget_id=9, period_start="2026-09-01", reservation_id="X" * 26)
    assert r["status"] == "reservation_missing"
