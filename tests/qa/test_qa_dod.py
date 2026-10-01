"""★ QA 独立 DoD 复验（D）——不依赖真实 MySQL 的部分。

覆盖：P0-1 span 树 / P0-2 埋点三态 / P0-3 成本 / P0-7 SSE / cost_curve 对照组 /
P2-9 rollup 幂等 / FIX-A / FIX-B。全部为**自行构造**的用例，非复跑工程师用例。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import pytest
from dba_runtime import (
    RunContext,
    bind_metering,
    flush_queue,
    metered,
    reset_current,
    set_current,
    traced,
)


# ═════════════════════════════════════════════════════════════════════════
# 收集器（自建，不复用工程师 conftest）
# ═════════════════════════════════════════════════════════════════════════
class _Collector:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self.llms: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.dlq: list[tuple[str, dict[str, Any]]] = []

    async def record_llm(self, rec: dict[str, Any]) -> None:
        self.llms.append(dict(rec))

    async def record_tool(self, rec: dict[str, Any]) -> None:
        self.tools.append(dict(rec))

    async def record_span(self, rec: dict[str, Any]) -> None:
        self.spans.append(dict(rec))

    async def record_dlq(self, kind: str, rec: dict[str, Any]) -> None:
        self.dlq.append((kind, dict(rec)))


# ═════════════════════════════════════════════════════════════════════════
# P0-1 span 树：60 并发，逐 trace 校验 parent_span_id（自建，非复用）
# ═════════════════════════════════════════════════════════════════════════
_N = 60


async def test_p0_1_span_tree_under_concurrency() -> None:
    col = _Collector()
    bind_metering(col)

    @traced("leaf", kind="tool")
    async def leaf() -> int:
        return 1

    @traced("mid", kind="agent")
    async def mid() -> int:
        return await leaf()

    @traced("root_op", kind="agent")
    async def root_op() -> int:
        return await mid()

    async def one(i: int) -> None:
        root_span = f"r{i:015d}"
        ctx = RunContext(trace_id=f"trace{i:027d}", span_id=root_span, module="system")
        token = set_current(ctx)
        try:
            await root_op()
        finally:
            reset_current(token)

    await asyncio.gather(*(one(i) for i in range(_N)))
    n = await flush_queue(col)
    assert n == _N * 3, f"应落 {_N * 3} 条 span，实得 {n}"

    by_trace: dict[str, dict[str, dict[str, Any]]] = {}
    for sp in col.spans:
        by_trace.setdefault(sp["trace_id"], {})[sp["name"]] = sp
    assert len(by_trace) == _N, "并发 trace 串扰"

    for i in range(_N):
        tr = f"trace{i:027d}"
        sp = by_trace[tr]
        assert sp["root_op"]["parent_span_id"] == f"r{i:015d}"
        assert sp["mid"]["parent_span_id"] == sp["root_op"]["span_id"]
        assert sp["leaf"]["parent_span_id"] == sp["mid"]["span_id"]
    ids = [s["span_id"] for s in col.spans]
    assert len(ids) == len(set(ids)), "span_id 重复"


# ═════════════════════════════════════════════════════════════════════════
# P0-2 埋点三态：构造一次超时调用，llm_call 有且仅有一条 status='timeout'
# ═════════════════════════════════════════════════════════════════════════
async def test_p0_2_timeout_records_single_timeout_llm() -> None:
    col = _Collector()
    bind_metering(col)

    @metered("llm")
    async def call() -> Any:
        raise TimeoutError("simulated llm timeout")

    root = RunContext(trace_id="7" * 32, span_id="8" * 16, module="chatbi")
    token = set_current(root)
    try:
        with pytest.raises(TimeoutError):
            await call()
    finally:
        reset_current(token)
    await flush_queue(col)

    assert len(col.llms) == 1, f"应恰好落 1 条 llm 记录，实得 {len(col.llms)}"
    assert col.llms[0]["status"] == "timeout"
    assert col.llms[0]["error_code"] is not None


async def test_p0_2_success_and_error_also_recorded() -> None:
    col = _Collector()
    bind_metering(col)

    @metered("llm")
    async def ok() -> Any:
        return None

    @metered("llm")
    async def bad() -> Any:
        raise ValueError("boom")

    root = RunContext(trace_id="9" * 32, span_id="a" * 16, module="chatbi")
    token = set_current(root)
    try:
        await ok()
        with pytest.raises(ValueError):
            await bad()
    finally:
        reset_current(token)
    await flush_queue(col)
    assert [r["status"] for r in col.llms] == ["ok", "error"]


# ═════════════════════════════════════════════════════════════════════════
# P0-3 成本：prompt=1000, cached=600 → 手算比对（400 计费输入）
# ═════════════════════════════════════════════════════════════════════════
def test_p0_3_cost_hand_computed() -> None:
    import datetime as dt

    from dba.capabilities.cost import CostNormalizer, PriceCache
    from dba.capabilities.cost.normalize import billable_input_tokens
    from dba.capabilities.cost.price_cache import PriceRow

    # 手算：可计费输入 = 1000-600 = 400
    #   输入 = 400 * 2500 / 1000 = 1000
    #   缓存 = 600 * 1250 / 1000 = 750
    #   输出 = 0
    #   合计 = 1750（v1 错值 = 1000*2500/1000 + 600*1250/1000 = 3250）
    price = PriceRow(
        provider="openai",
        model="gpt-4o",
        billing_unit="PER_1K_TOKEN",
        input_price_micro_usd=2500,
        output_price_micro_usd=10000,
        cache_read_price_micro_usd=1250,
        cache_write_price_micro_usd=0,
        currency="USD",
        fx_rate_to_usd=1.0,
        effective_from=dt.datetime(2026, 1, 1),
        effective_to=None,
        price_book_id=7,
    )
    assert billable_input_tokens(1000, 600) == 400
    cost = CostNormalizer(PriceCache()).normalize_with_price(  # type: ignore[arg-type]
        {
            "provider": "openai",
            "model": "gpt-4o",
            "prompt_tokens": 1000,
            "cached_tokens": 600,
            "completion_tokens": 0,
        },  # type: ignore[arg-type]
        price,
    )
    expected = (1000 - 600) * 2500 // 1000 + 600 * 1250 // 1000 + 0
    assert cost.total_micro_usd == expected == 1750
    assert cost.total_micro_usd != 3250


# ═════════════════════════════════════════════════════════════════════════
# P0-7 SSE：Last-Event-ID 重放，seq 无缺口无重复
# ═════════════════════════════════════════════════════════════════════════
async def test_p0_7_sse_replay_no_gap_no_dup() -> None:
    from dba.api.v1.stream import _stream, build_sse_frames

    _ = build_sse_frames  # 见下：直接验证纯函数
    from dba_runtime.events import EventEnvelope

    events = [
        EventEnvelope.build("agent.step.delta", 1, "t" * 32, {"i": 1}),
        EventEnvelope.build("agent.step.delta", 2, "t" * 32, {"i": 2}),
        EventEnvelope.build("agent.step.delta", 3, "t" * 32, {"i": 3}),
        EventEnvelope.build("sql.executed", 4, "t" * 32, {"i": 4}),
        EventEnvelope.build("run.finished", 5, "t" * 32, {"i": 5}),
    ]

    class _Pub:
        async def replay(self, trace_id: str, after_seq: int) -> list[Any]:  # noqa: ARG002
            return [e for e in events if e.seq > after_seq]

    # 纯函数：id 必须严格取事件自身 seq
    frames = build_sse_frames(events)
    assert frames[0].startswith("id: 1\n")
    assert "id: 5\n" in frames[4]

    # 续传：Last-Event-ID=3 → 只应得到 4、5
    agen = _stream(_Pub(), "t" * 32, 3)
    got: list[str] = []
    async for frame in agen:
        got.append(frame)
        if len(got) >= 2:
            break
    await agen.aclose()
    ids = [int(f.split("\n", 1)[0].split(": ", 1)[1]) for f in got]
    assert ids == [4, 5], f"续传应只补 4,5，实得 {ids}"


# ═════════════════════════════════════════════════════════════════════════
# cost_curve 对照组：返回里确实有 hit / miss 两组
# ═════════════════════════════════════════════════════════════════════════
def test_cost_curve_has_hit_and_miss_groups() -> None:
    from dba.modules.finops.cost_curve import CostCurveAnalyzer, DailyPoint

    pts = [
        DailyPoint(
            day=date(2026, 1, 1),
            reuse_rate=0.10,
            tokens_hit=1000,
            tokens_miss=1005,
            hit_samples=10,
            miss_samples=8,
        ),
        DailyPoint(
            day=date(2026, 1, 2),
            reuse_rate=0.50,
            tokens_hit=700,
            tokens_miss=1000,
            hit_samples=12,
            miss_samples=9,
        ),
        DailyPoint(
            day=date(2026, 1, 3),
            reuse_rate=0.90,
            tokens_hit=400,
            tokens_miss=990,
            hit_samples=11,
            miss_samples=7,
        ),
    ]
    data = CostCurveAnalyzer().analyze(biz_line_id=1, days=3, points=pts)
    groups = {row["group"] for row in data.token_series}
    assert groups == {"hit", "miss"}, f"必须带对照组，实得 {groups}"
    assert data.correlation is not None and data.correlation < 0
    # 未配置价格 → 不编造金额
    assert data.total_saved_micro_usd == 0


# ═════════════════════════════════════════════════════════════════════════
# P2-9 rollup 幂等：连跑两次，数值不翻倍
# ═════════════════════════════════════════════════════════════════════════
async def test_p2_9_rollup_idempotent() -> None:
    from dba.modules.observability.rollup import RollupService

    runs = [
        {
            "module": "chatbi",
            "biz_line_id": 1,
            "agent_uid": "ag1",
            "status": "success",
            "tokens_in": 100,
            "tokens_out": 50,
            "cached_tokens": 10,
            "cost_micro_usd": 500,
            "latency_ms": 120,
            "llm_calls": 1,
            "tool_calls": 0,
        },
        {
            "module": "chatbi",
            "biz_line_id": 1,
            "agent_uid": "ag1",
            "status": "success",
            "tokens_in": 200,
            "tokens_out": 60,
            "cached_tokens": 0,
            "cost_micro_usd": 700,
            "latency_ms": 80,
            "llm_calls": 1,
            "tool_calls": 1,
        },
        {
            "module": "observability",
            "biz_line_id": 1,
            "agent_uid": "self",
            "status": "success",
            "tokens_in": 9,
            "tokens_out": 9,
            "cached_tokens": 0,
            "cost_micro_usd": 9999,
            "latency_ms": 10,
            "llm_calls": 1,
            "tool_calls": 0,
        },
    ]

    class _RunRepo:
        async def list_runs(self, flt: dict[str, Any], limit: int) -> list[dict[str, Any]]:  # noqa: ARG002
            return runs

    class _MetricRepo:
        def __init__(self) -> None:
            self.batches: list[list[dict[str, Any]]] = []

        async def upsert_many(self, rows: list[dict[str, Any]]) -> None:
            self.batches.append([dict(r) for r in rows])

    mr = _MetricRepo()
    svc = RollupService(run_repo=_RunRepo(), metric_repo=mr)
    r1 = await svc.run(date(2026, 9, 20))
    await svc.run(date(2026, 9, 20))  # 第二次运行：验证幂等（结果由 mr.batches 断言）

    assert r1.self_run_count == 1, "平台自身模块应被排除"
    assert len(mr.batches) == 2
    b1, b2 = mr.batches[0], mr.batches[1]
    # 幂等：两次写入内容一致，run_count 不翻倍
    assert b1 == b2, "rollup 重跑结果不一致（不幂等）"
    row = b1[0]
    assert row["run_count"] == 2, f"run_count 不应翻倍，实得 {row['run_count']}"
    assert row["cost_micro_usd"] == 1200, f"成本被重复累加：{row['cost_micro_usd']}"


# ═════════════════════════════════════════════════════════════════════════
# FIX-A / FIX-B：构造 app，观测/审计链路故障不得 500
# ═════════════════════════════════════════════════════════════════════════
class _BoomProgress:
    def __init__(self) -> None:
        self.calls = 0

    async def write(self, trace_id: str, envelope: dict[str, Any]) -> None:  # noqa: ARG002
        self.calls += 1
        raise ConnectionError("redis down")


async def test_fix_b_probes_never_500_on_progress_failure() -> None:
    from dba.config import Settings
    from dba.main import create_app

    app = create_app(Settings(env="test"))
    boom = _BoomProgress()
    app.state.container.set("progress", boom)
    app.state.container.set("dispatcher", None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with app.router.lifespan_context(app):
            for path in ("/health", "/ready", "/metrics"):
                resp = await client.get(path)
                assert resp.status_code != 500, f"{path} 返回 500（应 fail-open）"
            assert boom.calls == 0, "探针路径不应产 run.started"


async def test_fix_a_non_skip_path_no_attribute_error() -> None:
    from dba.config import Settings
    from dba.main import create_app

    app = create_app(Settings(env="test"))
    app.state.container.set("dispatcher", None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with app.router.lifespan_context(app):
            # 非 skip 业务路径：无论鉴权结果如何，都绝不能是 500 / AttributeError
            for path in ("/api/v1/obs/overview", "/api/v1/finops/cost/summary"):
                resp = await client.get(path)
                assert resp.status_code != 500, f"{path} 返回 500（audit_sink 契约问题）"


def test_fix_a_event_index_repo_has_write() -> None:
    from dba.storage.es.repo import EventIndexRepo

    assert callable(getattr(EventIndexRepo, "write", None)), "EventIndexRepo 缺 write() 契约"
