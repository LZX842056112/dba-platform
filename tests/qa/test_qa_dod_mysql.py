"""★ QA 独立 DoD 复验（D·真实 MySQL）：P0-4 结算/释放幂等（3 连发）+ P0-5 HARD 档可达。"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from dba.capabilities.budget import BudgetService
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.repo import MysqlRepositories
from dba.storage.redis.client import RedisStorage
from dba.storage.redis.repo import RedisBudgetCache

pytestmark = pytest.mark.usefixtures("mysql_dsn")


async def _make_budget(repo: object, *, amount: int, scope_id: str, version: int = 1) -> int:
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
                "version": version,
                "created_at": dt.datetime(2026, 1, 1),
                "updated_at": dt.datetime(2026, 1, 1),
            }
        )
    )


@pytest.fixture
async def env(mysql_dsn: str):  # type: ignore[no-untyped-def]
    engine = build_engine(mysql_dsn)
    repos = MysqlRepositories(engine)
    redis = RedisStorage("redis://fake/0", use_fake=True)
    await redis.ensure_scripts()
    svc = BudgetService(
        budget_repo=repos.budget,
        usage_repo=repos.budget_usage,
        reservation_repo=repos.budget_reservation,
        redis_cache=RedisBudgetCache(redis),
        fail_open=True,
    )
    try:
        yield engine, repos, svc
    finally:
        await redis.aclose()
        await engine.dispose()


async def _resolve(svc, scope: str):  # type: ignore[no-untyped-def]
    return await svc.resolve(
        "GLOBAL", scope, "MONTH", at=dt.datetime(2026, 9, 15, tzinfo=dt.UTC)
    )


async def test_p0_4_settle_and_release_idempotent_triple(env) -> None:  # type: ignore[no-untyped-def]
    _engine, repos, svc = env
    scope = f"qa-p04-{uuid.uuid4().hex[:8]}"
    budget_id = await _make_budget(repos, amount=1_000_000, scope_id=scope)
    budget = await _resolve(svc, scope)
    assert budget is not None

    dec = await svc.reserve(budget, estimated_micro_usd=50_000, trace_id="q" * 32)
    rid = dec.reservation_id
    assert dec.decision == "allow" and rid is not None

    # settle 连发 3 次（后两次 actual 不同，均应被忽略）
    s1 = await svc.settle(budget, reservation_id=rid, actual_micro_usd=42_000)
    s2 = await svc.settle(budget, reservation_id=rid, actual_micro_usd=999_999)
    s3 = await svc.settle(budget, reservation_id=rid, actual_micro_usd=123)
    assert s1["status"] == "settled"
    assert s2["status"] == "already_settled"
    assert s3["status"] == "already_settled"

    usage = await repos.budget_usage.get(budget_id, budget.period_start)
    assert int(usage["consumed_micro_usd"]) == 42_000, f"consumed 被重复累加：{usage}"
    assert int(usage["reserved_micro_usd"]) == 0

    # release 连发 3 次（同一 rid，已 settled）
    r1 = await svc.release(budget, reservation_id=rid)
    r2 = await svc.release(budget, reservation_id=rid)
    r3 = await svc.release(budget, reservation_id=rid)
    assert r1["status"] in ("already_released", "already_settled", "released")
    assert r2["status"] != "released" and r3["status"] != "released"

    usage2 = await repos.budget_usage.get(budget_id, budget.period_start)
    assert int(usage2["consumed_micro_usd"]) == 42_000, "release 后 consumed 不应变化"

    ledger = await repos.budget_reservation.sum_reserved(budget_id, budget.period_start)
    assert ledger == 0


async def test_p0_4_release_only_idempotent_triple(env) -> None:  # type: ignore[no-untyped-def]
    _engine, repos, svc = env
    scope = f"qa-rel-{uuid.uuid4().hex[:8]}"
    budget_id = await _make_budget(repos, amount=500_000, scope_id=scope)
    budget = await _resolve(svc, scope)
    assert budget is not None

    dec = await svc.reserve(budget, estimated_micro_usd=30_000)
    rid = dec.reservation_id
    assert rid is not None

    outs = [await svc.release(budget, reservation_id=rid) for _ in range(3)]
    assert outs[0]["status"] == "released"
    assert outs[1]["status"] == "already_released"
    assert outs[2]["status"] == "already_released"

    usage = await repos.budget_usage.get(budget_id, budget.period_start)
    assert int(usage["consumed_micro_usd"]) == 0
    assert int(usage["reserved_micro_usd"]) == 0
    assert await repos.budget_reservation.sum_reserved(budget_id, budget.period_start) == 0


async def test_p0_5_hard_decision_reachable_at_90pct(env) -> None:  # type: ignore[no-untyped-def]
    _engine, repos, svc = env
    scope = f"qa-p05-{uuid.uuid4().hex[:8]}"
    await _make_budget(repos, amount=100_000, scope_id=scope)
    budget = await _resolve(svc, scope)
    assert budget is not None
    assert budget.hard_limit_micro_usd == 90_000, "hard_limit_pct 默认应为 90"

    decisions = [
        (await svc.reserve(budget, estimated_micro_usd=1_000)).decision for _ in range(120)
    ]
    allowed = sum(1 for d in decisions if d in ("allow", "soft"))
    blocked = sum(1 for d in decisions if d == "hard")
    assert allowed == 90, f"HARD 未在 90% 边界正确触发：allowed={allowed}"
    assert blocked == 30, f"blocked 数不符：{blocked}"
    # 明确验证 HARD 档（decision='hard'）确实可达
    assert "hard" in decisions


async def test_p0_5_db_default_hard_limit_pct_is_90(mysql_dsn: str) -> None:
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
