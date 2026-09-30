"""Pipeline 状态机单测：on_error='goto' 回退、timeout_s 超时、max_visit 上限。"""

from __future__ import annotations

import asyncio
from typing import Any

from dba_runtime import (
    AgentOutput,
    AgentRegistry,
    InMemoryEmitter,
    Pipeline,
    RunContext,
    Step,
    TransientSqlError,
)


class FnAgent:
    """按函数驱动的测试 Agent。"""

    def __init__(self, name: str, fn: Any) -> None:
        self.name = name
        self._fn = fn

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        result = self._fn(payload, ctx)
        if asyncio.iscoroutine(result):
            result = await result
        assert isinstance(result, AgentOutput)
        return result


def _registry(*agents: FnAgent) -> AgentRegistry:
    reg = AgentRegistry()
    for agent in agents:
        reg.register(agent.name, agent)
    return reg


def _ctx() -> RunContext:
    return RunContext(trace_id="c" * 32, span_id="root", module="chatbi")


async def test_goto_fallback_recovers() -> None:
    """on_error='goto'：失败一次后带反馈回退上游，第二次成功。"""
    calls = {"sql_guard": 0}

    async def intent(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        return AgentOutput(data={"intent": "query"})

    async def sql_gen(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        # 第一次生成的 SQL 被护栏拒绝；回退后拿到 _feedback 再生成正确 SQL
        if "_feedback" in payload:
            return AgentOutput(data={"sql": "select 1"})
        return AgentOutput(data={"sql": "select bad"})

    async def sql_guard(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        calls["sql_guard"] += 1
        if payload.get("sql") != "select 1":
            raise TransientSqlError("SQL 被护栏拒绝，需回退")
        return AgentOutput(data={"scope_injected": True})

    async def sql_exec(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        return AgentOutput(data={"row_count": 1})

    reg = _registry(
        FnAgent("intent", intent),
        FnAgent("sql_gen", sql_gen),
        FnAgent("sql_guard", sql_guard),
        FnAgent("sql_exec", sql_exec),
    )
    steps = [
        Step(name="intent", agent_name="intent", next="sql_gen", timeout_s=5),
        Step(
            name="sql_gen",
            agent_name="sql_gen",
            next="sql_guard",
            timeout_s=5,
            produces=("sql",),
        ),
        Step(
            name="sql_guard",
            agent_name="sql_guard",
            next="sql_exec",
            on_error="goto",
            goto_step="sql_gen",
            max_retries=1,
            timeout_s=5,
            produces=("scope_injected",),
        ),
        Step(name="sql_exec", agent_name="sql_exec", next=None, timeout_s=5),
    ]
    pipeline = Pipeline("chatbi", steps, entry="intent", registry=reg)
    emitter = InMemoryEmitter()

    result = await pipeline.execute({}, _ctx(), emitter)

    assert result.status == "success"
    assert result.steps_run == [
        "intent",
        "sql_gen",
        "sql_guard",
        "sql_gen",
        "sql_guard",
        "sql_exec",
    ]
    assert result.retries == 1
    assert result.output["sql"] == "select 1"
    assert result.output["row_count"] == 1
    # ★ v2：成功后 _feedback 必须被清理，不能污染后续步骤
    assert "_feedback" not in result.output
    # 回退时发出了 sql.validation.failed（唯一生产者是 Pipeline）
    assert "sql.validation.failed" in emitter.names()


async def test_step_timeout_fails() -> None:
    """on_timeout='fail'：单步超时必须真正生效并返回 timeout 终态。"""

    async def slow(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        await asyncio.sleep(5)
        return AgentOutput(data={"ok": True})

    reg = _registry(FnAgent("slow", slow))
    steps = [Step(name="slow", agent_name="slow", next=None, timeout_s=0.05, on_timeout="fail")]
    pipeline = Pipeline("t", steps, entry="slow", registry=reg)
    emitter = InMemoryEmitter()

    result = await pipeline.execute({}, _ctx(), emitter)

    assert result.status == "timeout"
    assert result.timeout_steps == ["slow"]
    assert result.error_code == "STEP_TIMEOUT"
    assert "50013" in [e.data.get("code") for e in emitter.events if e.event == "run.error"]


async def test_step_timeout_goto() -> None:
    """on_timeout='goto'：超时后回退到 goto_step 重试。"""
    attempts = {"n": 0}

    async def flaky(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        attempts["n"] += 1
        if attempts["n"] == 1:
            await asyncio.sleep(5)
        return AgentOutput(data={"ready": True})

    async def warmup(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        return AgentOutput(data={"warm": True})

    reg = _registry(FnAgent("flaky", flaky), FnAgent("warmup", warmup))
    steps = [
        Step(name="warmup", agent_name="warmup", next="flaky", timeout_s=5),
        Step(
            name="flaky",
            agent_name="flaky",
            next=None,
            timeout_s=0.05,
            on_timeout="goto",
            goto_step="warmup",
            max_retries=1,
        ),
    ]
    pipeline = Pipeline("t", steps, entry="warmup", registry=reg)
    result = await pipeline.execute({}, _ctx(), InMemoryEmitter())

    assert result.status == "success"
    assert result.retries == 1
    assert result.steps_run == ["warmup", "flaky", "warmup", "flaky"]


async def test_max_visit_ceiling() -> None:
    """goto 成环时，max_visit 快速失败，而不是打光 token 预算。"""

    async def noop(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        return AgentOutput(data={})

    reg = _registry(FnAgent("noop", noop))
    steps = [Step(name="loop", agent_name="noop", next="loop", max_visit=3, timeout_s=5)]
    pipeline = Pipeline("t", steps, entry="loop", registry=reg)

    result = await pipeline.execute({}, _ctx(), InMemoryEmitter())

    assert result.status == "failed"
    assert result.error_code == "STEP_VISIT_EXCEEDED"
    # steps_run 在 max_visit 检查之前 append（与冻结设计一致）：
    # 第 4 次访问被记入 steps_run 后立即判定超限。
    assert result.steps_run == ["loop", "loop", "loop", "loop"]


async def test_permission_denied_short_circuits() -> None:
    """越权类失败（SQL_PERMISSION_DENIED）绝不重试，立即失败。"""
    from dba_runtime import PermissionDeniedError

    attempts = {"n": 0}

    async def guard(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        attempts["n"] += 1
        raise PermissionDeniedError("该用户无权访问该数据域")

    reg = _registry(FnAgent("guard", guard))
    steps = [
        Step(
            name="guard",
            agent_name="guard",
            next=None,
            on_error="goto",
            goto_step="guard",
            max_retries=3,
            timeout_s=5,
        )
    ]
    pipeline = Pipeline("t", steps, entry="guard", registry=reg)

    result = await pipeline.execute({}, _ctx(), InMemoryEmitter())

    assert result.status == "failed"
    assert result.error_code == "SQL_PERMISSION_DENIED"
    assert attempts["n"] == 1  # 只跑一次，未触发回退
