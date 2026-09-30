"""B5 worker 任务单测（不依赖容器/真实存储：以替身覆盖决策与诚实降级路径）。

覆盖《实现要点清单》§1.6.6 与 DoD#4/#5，以及 P2-9（rollup 幂等）、P1-4（排除平台自身）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any

from dba.di import Container
from dba.modules.observability import RollupService
from dba_worker.jobs import (
    close_stale_runs,
    maintain_partitions,
    run_anomaly_scan,
    run_archive,
    run_daily_report,
    run_reconcile,
    run_rollup,
    run_semcache_gc,
)
from dba_worker.scheduler import with_run_context


# ── 替身 ────────────────────────────────────────────────────────────
class _FakeRunRepo:
    """内存版 ``run`` 仓储（支持 list / finish）。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_runs(self, flt: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self.rows:
            if flt.get("module") and row.get("module") != flt["module"]:
                continue
            if flt.get("biz_line_id") is not None and row.get("biz_line_id") != flt["biz_line_id"]:
                continue
            if flt.get("status") and row.get("status") != flt["status"]:
                continue
            if flt.get("since") is not None and row["started_at"] < flt["since"]:
                continue
            if flt.get("until") is not None and row["started_at"] >= flt["until"]:
                continue
            out.append(row)
        return out[:limit]

    async def finish(self, trace_id: str, *, started_at: Any, row: dict[str, Any]) -> int:
        for item in self.rows:
            if item["trace_id"] == trace_id and item["started_at"] == started_at:
                item.update(row)
                return 1
        return 0


class _FakeMetricRepo:
    """内存版 ``metric_daily`` 仓储（按主键 upsert，验证幂等）。"""

    def __init__(self) -> None:
        self.by_pk: dict[tuple[Any, ...], dict[str, Any]] = {}

    async def upsert_many(self, rows: list[dict[str, Any]]) -> int:
        for row in rows:
            pk = (row["stat_date"], row["biz_line_id"], row["agent_uid"], row["model"])
            self.by_pk[pk] = dict(row)
        return len(rows)

    @property
    def cost_total(self) -> int:
        return sum(int(r["cost_micro_usd"]) for r in self.by_pk.values())


class _FakeRegistry:
    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def has(self, name: str) -> bool:
        return name == "anomaly"

    def get(self, name: str) -> Any:
        return self._agent


class _FakeAnomalyAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, payload: dict[str, Any], run_ctx: Any) -> Any:
        self.calls.append(payload)
        return SimpleNamespace(data={"alert_count": 2, "alerts": []})


class _FakeObsService:
    async def overview(
        self, *, since: Any, until: Any, biz_line_id: int | None = None
    ) -> dict[str, Any]:
        return {"kpi_cards": {"silent_failures": {"value": 4}}}

    async def skill_metrics(self, stat_date: Any, biz_line_id: Any) -> Any:
        return SimpleNamespace(total=5, used=3, reuse_rate=0.6, dead_count=1)

    async def memory_metrics(self, stat_date: Any, biz_line_id: Any) -> Any:
        return SimpleNamespace(hit_rate=0.5, lookups=10, hits=5)

    async def anomalies(
        self, *, status: Any = None, category: Any = None, severity: Any = None
    ) -> list[dict[str, Any]]:
        return [{"id": 1}, {"id": 2}]


class _FakeSemCache:
    def __init__(self, n: int) -> None:
        self.n = n
        self.called_with: Any = None

    async def delete_expired(self, now: Any) -> int:
        self.called_with = now
        return self.n


# ── rollup（P2-9 幂等 + P1-4 排除自身）───────────────────────────────
async def test_rollup_job_idempotent_and_excludes_self_modules() -> None:
    day = datetime(2026, 9, 1, 1, 0)
    rows = [
        {
            "trace_id": "a" * 32,
            "started_at": day,
            "module": "chatbi",
            "biz_line_id": 1,
            "agent_uid": "u1",
            "status": "success",
            "tokens_in": 10,
            "tokens_out": 5,
            "cached_tokens": 0,
            "cost_micro_usd": 100,
            "latency_ms": 200,
            "llm_calls": 1,
            "tool_calls": 0,
        },
        {
            "trace_id": "b" * 32,
            "started_at": day,
            "module": "chatbi",
            "biz_line_id": 1,
            "agent_uid": "u1",
            "status": "failed",
            "tokens_in": 3,
            "tokens_out": 1,
            "cached_tokens": 0,
            "cost_micro_usd": 50,
            "latency_ms": 100,
            "llm_calls": 1,
            "tool_calls": 0,
        },
        # 平台自身 Run（observability）——必须被排除，不进业务成本
        {
            "trace_id": "c" * 32,
            "started_at": day,
            "module": "observability",
            "biz_line_id": 0,
            "agent_uid": "",
            "status": "success",
            "tokens_in": 999,
            "tokens_out": 999,
            "cached_tokens": 0,
            "cost_micro_usd": 7777,
            "latency_ms": 9,
            "llm_calls": 1,
            "tool_calls": 0,
        },
    ]
    run_repo = _FakeRunRepo(rows)
    metric_repo = _FakeMetricRepo()
    rollup = RollupService(run_repo=run_repo, metric_repo=metric_repo)
    container = Container()
    container.set("observability", SimpleNamespace(rollup=rollup))

    first = await run_rollup(container, stat_date="2026-09-01")
    cost_after_first = metric_repo.cost_total
    second = await run_rollup(container, stat_date="2026-09-01")

    assert first["ok"] and second["ok"]
    assert first["total_rows"] == second["total_rows"]  # 幂等：行数不翻倍
    assert cost_after_first == metric_repo.cost_total == 150  # 7777 被排除，重跑不累加
    assert first["reports"][0]["self_run_count"] == 1


# ── stale_run（DoD#5）────────────────────────────────────────────────
async def test_stale_run_closes_only_overdue_running() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[dict[str, Any]] = [
        {"trace_id": "d" * 32, "started_at": now - timedelta(hours=1), "status": "running"},
        {"trace_id": "e" * 32, "started_at": now - timedelta(minutes=1), "status": "running"},
    ]
    container = Container()
    container.set("repos", SimpleNamespace(run=_FakeRunRepo(rows)))

    out = await close_stale_runs(container, threshold_minutes=15)

    assert out["ok"] and out["closed"] == 1
    assert rows[0]["status"] == "timeout"
    assert rows[0]["error_code"] == "STALE_RUN_TIMEOUT"
    assert rows[1]["status"] == "running"  # 未超阀值：不动


# ── semcache_gc ──────────────────────────────────────────────────────
async def test_semcache_gc_delegates_delete_expired() -> None:
    fake = _FakeSemCache(3)
    container = Container()
    container.set("mongo_repos", SimpleNamespace(semantic_cache_entry=fake))

    out = await run_semcache_gc(container)

    assert out["ok"] and out["deleted"] == 3
    assert fake.called_with is not None


# ── 诚实降级路径 ──────────────────────────────────────────────────────
async def test_archive_requires_storage() -> None:
    container = Container()
    container.set("repos", SimpleNamespace())
    container.set("mysql", None)

    out = await run_archive(container)

    assert out["ok"] is False and out["reason"] == "storage_unavailable"


async def test_partition_skips_without_mysql() -> None:
    container = Container()
    container.set("mysql", None)

    out = await maintain_partitions(container)

    assert out["ok"] is False and out["reason"] == "mysql_unavailable"


async def test_anomaly_scan_requires_module() -> None:
    container = Container()
    container.set("observability", None)

    out = await run_anomaly_scan(container)

    assert out["ok"] is False and out["reason"] == "anomaly_agent_not_assembled"


async def test_anomaly_scan_runs_under_run_context() -> None:
    agent = _FakeAnomalyAgent()
    container = Container()
    container.set("repos", None)
    container.set("observability", SimpleNamespace(registry=_FakeRegistry(agent)))

    # 经 ``with_run_context`` 包裹，验证任务内 ``ctx()`` 可用（P1-6）
    job = with_run_context(partial(run_anomaly_scan, container))
    out = await job()

    assert out["ok"] and out["alert_count"] == 2
    assert agent.calls and agent.calls[0] == {"hours": 24}  # 哨兵 0 → 全局扫描


# ── report ───────────────────────────────────────────────────────────
async def test_daily_report_not_persisted_without_store() -> None:
    container = Container()
    container.set("observability_service", _FakeObsService())
    container.set("object_store", None)

    out = await run_daily_report(container, stat_date="2026-09-01")

    assert out["ok"] and out["persisted"] is False
    assert out["report"]["silent_failures"] == 4
    assert out["report"]["open_anomalies"] == 2


# ── reconcile（DoD#4）────────────────────────────────────────────────
async def test_reconcile_within_tolerance(monkeypatch: Any) -> None:
    import dba_worker.jobs.reconcile as rec

    async def fake_detail(container: Any, start: Any, end: Any) -> int:
        return 1000

    async def fake_budgets(container: Any) -> list[dict[str, Any]]:
        return [{"id": 1, "scope_type": "GLOBAL", "scope_id": "g"}]

    monkeypatch.setattr(rec, "_detail_sum", fake_detail)
    monkeypatch.setattr(rec, "_enabled_budgets", fake_budgets)

    class _UsageRepo:
        async def get(self, bid: int, ps: Any) -> dict[str, Any]:
            return {"consumed_micro_usd": 1000}

    container = Container()
    container.set("repos", SimpleNamespace(budget_usage=_UsageRepo()))
    container.set("redis", None)
    container.set("budget", None)

    out = await run_reconcile(container)

    assert out["within_tolerance"] is True
    assert out["redis_comparable"] is False  # Redis 不可用：不纳入比较（避免假漂移）
    assert out["errors"] == {"mysql_vs_detail": 0.0}


async def test_reconcile_drift_writes_alert(monkeypatch: Any) -> None:
    import dba_worker.jobs.reconcile as rec

    async def fake_detail(container: Any, start: Any, end: Any) -> int:
        return 1000

    async def fake_budgets(container: Any) -> list[dict[str, Any]]:
        return [{"id": 1}]

    monkeypatch.setattr(rec, "_detail_sum", fake_detail)
    monkeypatch.setattr(rec, "_enabled_budgets", fake_budgets)

    class _UsageRepo:
        async def get(self, bid: int, ps: Any) -> dict[str, Any]:
            return {"consumed_micro_usd": 2000}  # 100% 漂移

    inserted: list[dict[str, Any]] = []

    class _AlertRepo:
        async def insert(self, row: dict[str, Any]) -> int:
            inserted.append(row)
            return 42

    container = Container()
    container.set("repos", SimpleNamespace(budget_usage=_UsageRepo(), alert_event=_AlertRepo()))
    container.set("redis", None)
    container.set("budget", None)

    out = await run_reconcile(container)

    assert out["within_tolerance"] is False
    assert out["alert_id"] == 42
    assert inserted and inserted[0]["metric"] == "reconcile_drift"
