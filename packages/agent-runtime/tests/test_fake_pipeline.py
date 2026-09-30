"""★ fake LLM 客户端驱动一次完整 Pipeline 跑通（不连任何数据库）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from conftest import CollectingMetering
from dba_runtime import (
    AgentOutput,
    AgentRegistry,
    InMemoryEmitter,
    LLMResult,
    ModelChoice,
    Pipeline,
    RunContext,
    Step,
    bind_metering,
    flush_queue,
    metered,
)
from dba_runtime.llm import FakeLLMClient


class FakeRouter:
    """基于 FakeLLMClient 的最小路由（内核单测替身）。"""

    def __init__(self, client: FakeLLMClient) -> None:
        self._client = client

    async def route(
        self,
        *,
        task: str,
        ctx: RunContext,
        quality: str | None = None,
    ) -> ModelChoice:
        _ = task, ctx, quality
        return ModelChoice(provider="fake", model="fake-1", max_tokens=64)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        ctx: RunContext,
        **kwargs: Any,
    ) -> LLMResult:
        return await self._client.complete(messages, choice, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        ctx: RunContext,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        async for chunk in self._client.stream(messages, choice, **kwargs):
            yield chunk


class SqlGenAgent:
    name = "sql_gen"

    def __init__(self, router: FakeRouter) -> None:
        self.router = router

    @metered("llm")
    async def _call(
        self, messages: list[dict[str, Any]], choice: ModelChoice, ctx: RunContext
    ) -> LLMResult:
        return await self.router.chat(messages, choice, ctx)

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        choice = await self.router.route(task="sql_gen", ctx=ctx)
        result = await self._call([{"role": "user", "content": payload.get("q", "")}], choice, ctx)
        return AgentOutput(data={"sql": result.text}, confidence=0.9)


class NarratorAgent:
    name = "narrator"

    def __init__(self, router: FakeRouter) -> None:
        self.router = router

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        choice = await self.router.route(task="narrator", ctx=ctx)
        pieces: list[str] = []
        emitter = ctx.emitter
        assert emitter is not None  # Pipeline 已注入
        messages = [{"role": "user", "content": "narrate"}]
        async for chunk in self.router.stream(messages, choice, ctx):
            pieces.append(chunk)
            await emitter.emit("narration.delta", {"text_delta": chunk})
        return AgentOutput(data={"narration": "".join(pieces)})


@pytest.mark.asyncio
async def test_fake_llm_drives_full_pipeline(metering: CollectingMetering) -> None:
    bind_metering(metering)

    client = FakeLLMClient(responses=["select 1 as gmv"], stream_chunks=["GMV", " 是 ", "1"])
    router = FakeRouter(client)

    reg = AgentRegistry()
    reg.register("sql_gen", SqlGenAgent(router))
    reg.register("narrator", NarratorAgent(router))

    steps = [
        Step(name="sql_gen", agent_name="sql_gen", next="narrator", timeout_s=5, produces=("sql",)),
        Step(name="narrator", agent_name="narrator", next=None, timeout_s=5),
    ]
    pipeline = Pipeline("chatbi-mini", steps, entry="sql_gen", registry=reg)
    emitter = InMemoryEmitter()
    ctx = RunContext(trace_id="f" * 32, span_id="root", module="chatbi", biz_line_id=12)

    result = await pipeline.execute({"q": "华东 GMV"}, ctx, emitter)

    assert result.status == "success"
    assert result.output["sql"] == "select 1 as gmv"
    assert result.output["narration"] == "GMV 是 1"

    names = emitter.names()
    assert names[0] == "agent.step.started"
    assert "narration.delta" in names
    assert names.count("narration.delta") == 3

    # ★ 一次 LLM 调用落一条记录（P0-2：成功路径）
    n = await flush_queue(metering)
    assert n == 1
    rec = metering.llms[0]
    assert rec["status"] == "ok"  # ★ NOT NULL 字段必须存在
    assert rec["model"] == "fake-1"
    assert rec["trace_id"] == "f" * 32
    assert rec["biz_line_id"] == 12
    assert rec["prompt_tokens"] == 10
    assert rec["usage_source"] == "measured"
    assert "cost_micro_usd" in rec and "price_book_id" in rec


@pytest.mark.asyncio
async def test_metered_records_failure(metering: CollectingMetering) -> None:
    """★ P0-2：失败的 LLM 调用也必须落一条记录，且 status='error'。"""
    bind_metering(metering)

    client = FakeLLMClient(fail_with=RuntimeError("upstream 500"), fail_times=1)
    router = FakeRouter(client)
    agent = SqlGenAgent(router)

    reg = AgentRegistry()
    reg.register("sql_gen", agent)
    steps = [Step(name="sql_gen", agent_name="sql_gen", next=None, timeout_s=5, on_error="fail")]
    pipeline = Pipeline("t", steps, entry="sql_gen", registry=reg)

    ctx = RunContext(trace_id="e" * 32, span_id="root", module="chatbi")
    result = await pipeline.execute({"q": "x"}, ctx, InMemoryEmitter())

    assert result.status == "failed"
    await flush_queue(metering)
    assert len(metering.llms) == 1
    assert metering.llms[0]["status"] == "error"
    assert metering.llms[0]["error_code"] == "INTERNAL_ERROR"
