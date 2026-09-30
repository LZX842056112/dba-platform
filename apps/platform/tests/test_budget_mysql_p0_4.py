"""★ DoD: P0-4/P0-5/P1-5/P1-9/U15 端到端（真实 MySQL 权威账本 + fakeredis 快路径）。

需要 ``DBA_TEST_MYSQL_DSN`` 且库已 ``dba migrate``（未设置则整文件 skip）。
验证：
  * 结算/释放**幂等**（重复调用不改 ``budget_usage``）；
  * 200 并发预留不超卖，且 ``budget_reservation``（RESERVED 合计）与 Redis reserved **一致**；
  * U15：``resolve`` 取 ``version`` 最大的生效预算；
  * P1-5：``self_reported`` 结算 = 释放（不计入 consumed）；
  * P0-5：DB 默认 ``hard_limit_pct=90``。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.repo import MysqlRepositories
from dba.storage.redis.client import RedisStorage
from dba.storage.redis.repo import RedisBudgetCache

pytestmark = pytest.mark.usefixtures("mysql_dsn")


async def _make_budget(engine: object, repo: object, *, amount: int, scope_id: str) -> int:
    return int(
        await repo.budget.upsert(  # type: ignore[attr-defined]
            {
                "scope_type": "GLOBAL",
                "scope_id": scope_id,
                "period": "MONTH",
                "amount_micro_usd": amount,
                "soft_limit_pct": 80,
                "hard_limit_pct": 90,
                "soft_action": "ALERT",
                "hard_action": "BLOCK",
                "priority": 100,
                "timezone": "Asia/Shanghai",
                "enabled": 1,
                "version": 1,
                "created_at": dt.datetime(2026, 1, 1),
                "updated_at": dt.datetime(2026, 1, 1),
            }
        )
    )


@pytest.fixture
async def env(mysql_dsn: str) -> object:
    engine = build_engine(mysql_dsn)
    repos = MysqlRepositories(engine)
    redis = RedisStorage("redis://fake/0", use_fake=True)
    await redis.ensure_scripts()
    try:
        yield engine, repos, redis
    finally:
        await redis.aclose()
        await engine.dispose()


def _service(repos: object, redis: object) -> object:
    from dba.capabilities.budget import BudgetService

    return BudgetService(
        budget_repo=repos.budget,  # type: ignore[attr-defined]
        usage_repo=repos.budget_usage,  # type: ignore[attr-defined]
        reservation_repo=repos.budget_reservation,  # type: ignore[attr-defined]
        redis_cache=RedisBudgetCache(redis),  # type: ignore[arg-type]
        fail_open=True,
    )


async def test_settle_idempotent_end_to_end(env: object) -> None:
    engine, repos, redis = env  # type: ignore[misc]
    svc = _service(repos, redis)
    scope = f"p04-{uuid.uuid4().hex[:8]}"
    budget_id = await _make_budget(engine, repos, amount=1_000_000, scope_id=scope)

    budget = await svc.resolve("GLOBAL", scope, "MONTH", at=dt.datetime(2026, 9, 15, tzinfo=dt.UTC))  # type: ignore[attr-defined]
    assert budget is not None

    decision = await svc.reserve(budget, estimated_micro_usd=50_000, trace_id="t" * 32)  # type: ignore[attr-defined]
    assert decision.decision == "allow"
    rid = decision.reservation_id
    assert rid is not None

    first = await svc.settle(budget, reservation_id=rid, actual_micro_usd=42_000)  # type: ignore[attr-defined]
    assert first["status"] == "settled"
    usage_after_1 = await repos.budget_usage.get(budget_id, budget.period_start)
    assert usage_after_1 is not None
    assert int(usage_after_1["consumed_micro_usd"]) == 42_000
    assert int(usage_after_1["reserved_micro_usd"]) == 0

    # ★ 二次结算：幂等，不改数
    second = await svc.settle(budget, reservation_id=rid, actual_micro_usd=999_999)  # type: ignore[attr-defined]
    assert second["status"] == "already_settled"
    usage_after_2 = await repos.budget_usage.get(budget_id, budget.period_start)
    assert int(usage_after_2["consumed_micro_usd"]) == 42_000  # 未被 999999 覆盖


async def test_release_idempotent_end_to_end(env: object) -> None:
    engine, repos, redis = env  # type: ignore[misc]
    svc = _service(repos, redis)
    scope = f"rel-{uuid.uuid4().hex[:8]}"
    budget_id = await _make_budget(engine, repos, amount=500_000, scope_id=scope)
    budget = await svc.resolve("GLOBAL", scope, "MONTH", at=dt.datetime(2026, 9, 15, tzinfo=dt.UTC))  # type: ignore[attr-defined]
    assert budget is not None

    decision = await svc.reserve(budget, estimated_micro_usd=30_000)  # type: ignore[attr-defined]
    rid = decision.reservation_id
    assert rid is not None and decision.decision == "allow"

    r1 = await svc.release(budget, reservation_id=rid)  # type: ignore[attr-defined]
    assert r1["status"] == "released"
    r2 = await svc.release(budget, reservation_id=rid)  # type: ignore[attr-defined]
    assert r2["status"] == "already_released"

    # 预留账本无 RESERVED 残留
    ledger = await repos.budget_reservation.sum_reserved(budget_id, budget.period_start)
    assert ledger == 0
    usage = await repos.budget_usage.get(budget_id, budget.period_start)
    assert int(usage["consumed_micro_usd"]) == 0
    assert int(usage["reserved_micro_usd"]) == 0


async def test_200_concurrent_reserve_consistent_redis_and_mysql(env: object) -> None:
    engine, repos, redis = env  # type: ignore[misc]
    svc = _service(repos, redis)
    scope = f"conc-{uuid.uuid4().hex[:8]}"
    amount_micro = 100_000
    budget_id = await _make_budget(engine, repos, amount=amount_micro, scope_id=scope)
    budget = await svc.resolve("GLOBAL", scope, "MONTH", at=dt.datetime(2026, 9, 15, tzinfo=dt.UTC))  # type: ignore[attr-defined]
    assert budget is not None
    # ★ P0-5：hard_limit_pct=90 → hard = 90000；每笔 1000 → 恰好 90 笔通过
    assert budget.hard_limit_micro_usd == 90_000

    async def one(_i: int) -> str:
        d = await svc.reserve(budget, estimated_micro_usd=1_000, trace_id="c" * 32)  # type: ignore[attr-defined]
        return d.decision

    results = await asyncio.gather(*(one(i) for i in range(200)))
    allowed = sum(1 for r in results if r in ("allow", "soft"))
    blocked = sum(1 for r in results if r == "hard")
    assert allowed == 90, f"超卖/漏放：allowed={allowed}"
    assert blocked == 110

    # ★ Redis ↔ MySQL 一致性：RESERVED 合计 == Redis reserved
    ledger = await repos.budget_reservation.sum_reserved(budget_id, budget.period_start)
    cache = RedisBudgetCache(redis)
    cached = await cache.usage(budget_id, "2026-09-01")
    assert ledger == 90_000  # 90 笔 * 1000 == hard_limit(90000)
    assert cached["reserved"] == ledger


async def test_u15_effective_uses_max_version(env: object) -> None:
    engine, repos, redis = env  # type: ignore[misc]
    svc = _service(repos, redis)
    scope = f"u15-{uuid.uuid4().hex[:8]}"
    await _make_budget(engine, repos, amount=100_000, scope_id=scope)
    # 新版本（更大额度）应生效
    await repos.budget.upsert(
        {
            "scope_type": "GLOBAL",
            "scope_id": scope,
            "period": "MONTH",
            "amount_micro_usd": 900_000,
            "soft_limit_pct": 80,
            "hard_limit_pct": 90,
            "soft_action": "ALERT",
            "hard_action": "BLOCK",
            "priority": 100,
            "timezone": "Asia/Shanghai",
            "enabled": 1,
            "version": 2,
            "created_at": dt.datetime(2026, 1, 1),
            "updated_at": dt.datetime(2026, 1, 1),
        }
    )
    budget = await svc.resolve("GLOBAL", scope, "MONTH", at=dt.datetime(2026, 9, 15, tzinfo=dt.UTC))  # type: ignore[attr-defined]
    assert budget is not None
    assert budget.version == 2
    assert budget.amount_micro_usd == 900_000


async def test_p1_5_self_reported_not_counted(env: object) -> None:
    engine, repos, redis = env  # type: ignore[misc]
    svc = _service(repos, redis)
    scope = f"p15-{uuid.uuid4().hex[:8]}"
    budget_id = await _make_budget(engine, repos, amount=200_000, scope_id=scope)
    budget = await svc.resolve("GLOBAL", scope, "MONTH", at=dt.datetime(2026, 9, 15, tzinfo=dt.UTC))  # type: ignore[attr-defined]
    assert budget is not None

    decision = await svc.reserve(budget, estimated_micro_usd=20_000)  # type: ignore[attr-defined]
    rid = decision.reservation_id
    assert rid is not None

    out = await svc.settle(  # type: ignore[attr-defined]
        budget, reservation_id=rid, actual_micro_usd=18_000, usage_source="self_reported"
    )
    assert out["reason"] == "self_reported_not_counted"
    usage = await repos.budget_usage.get(budget_id, budget.period_start)
    assert int(usage["consumed_micro_usd"]) == 0  # ★ 不计入预算


async def test_p0_5_hard_limit_default_90(mysql_dsn: str) -> None:
    engine = build_engine(mysql_dsn)
    try:
        async with engine.connect() as conn:
            default = (
                await conn.execute(
                    sa.text(
                        "SELECT COLUMN_DEFAULT FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='budget' "
                        "AND COLUMN_NAME='hard_limit_pct'"
                    )
                )
            ).scalar()
        assert str(default) == "90"
    finally:
        await engine.dispose()
