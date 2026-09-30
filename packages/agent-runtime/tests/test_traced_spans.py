"""★ P0-1：50 并发 Run 的 span.parent_span_id 全部正确（验证 span 树成立）。"""

from __future__ import annotations

import asyncio

import pytest
from conftest import CollectingMetering
from dba_runtime import RunContext, bind_metering, flush_queue, reset_current, set_current, traced

N_RUNS = 50


def _root_span_id(i: int) -> str:
    return f"root{i:012d}"  # 16 字符


@pytest.mark.asyncio
async def test_50_concurrent_runs_span_tree(metering: CollectingMetering) -> None:
    bind_metering(metering)

    @traced("inner", kind="tool")
    async def inner() -> int:
        return 1

    @traced("outer", kind="agent")
    async def outer() -> int:
        return await inner()

    async def one_run(i: int) -> None:
        root = RunContext(trace_id=f"{i:032x}", span_id=_root_span_id(i), module="system")
        token = set_current(root)
        try:
            await outer()
        finally:
            reset_current(token)

    await asyncio.gather(*(one_run(i) for i in range(N_RUNS)))

    n = await flush_queue(metering)
    assert n == N_RUNS * 2  # 每个 Run 两个 span（outer + inner）

    by_trace: dict[str, dict[str, dict]] = {}
    for span in metering.spans:
        by_trace.setdefault(span["trace_id"], {})[span["name"]] = dict(span)

    assert len(by_trace) == N_RUNS  # 无上下文串扰

    for i in range(N_RUNS):
        trace = f"{i:032x}"
        spans = by_trace[trace]
        outer_span = spans["outer"]
        inner_span = spans["inner"]
        # outer 挂在本次 Run 的根 span 上
        assert outer_span["parent_span_id"] == _root_span_id(i)
        # inner 挂在 outer 上（若 v1 未 set_current，这里会退化成 parent=root）
        assert inner_span["parent_span_id"] == outer_span["span_id"]
        # 所有 span 属于同一次 Run，且 span_id 唯一
        assert outer_span["span_id"] != inner_span["span_id"]
        assert outer_span["trace_id"] == inner_span["trace_id"] == trace

    # 所有 span_id 全局唯一
    span_ids = [s["span_id"] for s in metering.spans]
    assert len(span_ids) == len(set(span_ids))

    # start_ms 是绝对墙钟（毫秒级），不是 perf_counter
    for span in metering.spans:
        assert span["start_ms"] > 1_600_000_000_000  # > 2020-09-13（远大于任何 perf_counter 值）
