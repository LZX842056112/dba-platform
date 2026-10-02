"""Pipeline / Step 状态机与自愈回退（内核 L2，本方案的编排核心）。

对齐《设计方案 v2》§6.1.3 / 《实现要点清单》§5.3、§5.9、§6.5、U2、U11。

设计要点
--------
本模块用不到 120 行实现「确定性流水线 + 原地重试 + 带反馈回退 + 越权短路」四种语义。
与图编排框架的区别：不需要把流程表达成 DAG，只要一张 ``{step_name: Step}`` 表加一个入口名，
回退就是 ``state = step.goto_step``。

★ U2：``StepHook`` 与 ``_classify`` 在文档中被引用却未定义，此处补全。
★ 单步超时必须真正生效（v1 声明了 ``timeout_s`` 却从未使用）。
★ 成功即清 ``_feedback``（否则污染后续所有步骤）。
★ 回退前清理失败步骤 ``produces`` 声明的产物。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from .context import RunContext, reset_current, set_current
from .errors import classify as _classify
from .events import EventEmitter
from .registry import AgentRegistry

__all__ = ["Step", "PipelineResult", "Pipeline", "StepHook", "_classify"]

# ★ U2：StepHook 签名（文档只调用未定义）
StepHook = Callable[[str, dict[str, Any], RunContext, Exception | None], Awaitable[None]]


class Step(BaseModel):
    """流水线中的一个步骤。"""

    name: str
    agent_name: str  # 指向 AgentRegistry 中注册的名字
    next: str | None = None  # 下一步；None 表示流程结束
    on_error: Literal["fail", "retry", "goto"] = "fail"
    #  ★ 'goto' = 回退到指定步骤（自愈回退的核心）
    goto_step: str | None = None
    max_retries: int = 2
    timeout_s: float = 60.0
    # ★ v2：单步超时后的语义（v1 声明了 timeout_s 却从不使用）
    on_timeout: Literal["fail", "goto"] = "fail"
    # ★ v2：同一步最多被访问次数，兜住「goto 环」这类配置错误
    max_visit: int = 5
    # 该步产出的字段名（用于错误时只回填必要的 feedback，而不是整包重传）
    produces: tuple[str, ...] = ()


class PipelineResult(BaseModel):
    """流水线终态。``status='timeout'`` 是 ★ v2 引入的独立终态。"""

    status: Literal["success", "failed", "aborted", "timeout"]
    output: dict[str, Any] = Field(default_factory=dict)
    steps_run: list[str] = Field(default_factory=list)
    retries: int = 0
    timeout_steps: list[str] = Field(default_factory=list)  # ★ v2
    error_code: str | None = None
    error_message: str | None = None


class Pipeline:
    """确定性状态机 + 自愈回退。"""

    def __init__(self, name: str, steps: list[Step], entry: str, registry: AgentRegistry) -> None:
        self.name = name
        self.steps: dict[str, Step] = {s.name: s for s in steps}
        self.entry = entry
        self.registry = registry
        self._hooks: list[tuple[str, StepHook]] = []

        if entry not in self.steps:
            raise ValueError(f"Pipeline '{name}' 的入口步骤 '{entry}' 不在步骤表中")
        for step in steps:
            if step.next is not None and step.next not in self.steps:
                raise ValueError(f"步骤 '{step.name}'.next 指向未定义的 '{step.next}'")
            if step.goto_step is not None and step.goto_step not in self.steps:
                raise ValueError(f"步骤 '{step.name}'.goto_step 指向未定义的 '{step.goto_step}'")

    def on(self, event: Literal["before", "after", "error"], handler: StepHook) -> Pipeline:
        """注册生命周期钩子。埋点、审计、事件推送都通过钩子挂上，不写进 Agent 内部。"""
        self._hooks.append((event, handler))
        return self

    async def _fire(
        self,
        event: Literal["before", "after", "error"],
        state: str,
        data: dict[str, Any],
        ctx: RunContext,
        exc: Exception | None = None,
    ) -> None:
        """触发同类型钩子（顺序执行；钩子异常不阻断主流程，但会向上冒泡——由钩子自行兜底）。"""
        for registered, handler in self._hooks:
            if registered == event:
                await handler(state, data, ctx, exc)

    async def execute(
        self, payload: dict[str, Any], ctx: RunContext, emitter: EventEmitter
    ) -> PipelineResult:
        """执行流水线。

        ★ 在入口 ``set_current(ctx)``、出口 ``reset_current``：§6.1.1 明确「Pipeline 在入口
        set_current(ctx)」，这样被 ``@traced`` / ``@metered`` 包裹的 Agent 内部代码才能通过
        ``ctx()`` 读到本次 Run 的上下文（否则装饰器读到的可能是父上下文或未设置）。
        放在本层统一处理，避免每个 Agent 各自埋 set/reset。
        """
        # ★ v2：把 emitter 注入上下文，Agent 才能通过 ctx.emitter 发 run 级流式事件
        effective = ctx if ctx.emitter is not None else ctx.with_emitter(emitter)
        token = set_current(effective)
        try:
            return await self._run(payload, effective, emitter)
        finally:
            reset_current(token)

    async def _run(
        self, payload: dict[str, Any], ctx: RunContext, emitter: EventEmitter
    ) -> PipelineResult:
        """状态机主循环（``while state is not None``）。

        输入：``payload`` 初始数据包（步骤间以 ``data.update(out.data)`` 累积）、
              ``ctx``（携带 deadline / cancel_token）、``emitter``（事件出口）。
        输出：``PipelineResult``——``success`` / ``failed`` / ``aborted`` / ``timeout``。

        每轮循环依次检查（**顺序即优先级**）：
          1. 整次 Run 墙钟预算超时 → ``timeout``（防「各步都不超时但总时长爆炸」）；
          2. 协作式取消信号 → ``aborted``；
          3. 单步访问次数上限 → ``failed``（防 ``goto`` 配成环打光 token）；
          4. 执行步骤（含单步 ``asyncio.timeout``）。

        失败分支：
          * ``SQL_PERMISSION_DENIED`` → **短路不重试**（防止模型试错绕过权限）；
          * ``on_error="retry"`` → 原地重试；
          * ``on_error="goto"`` → 清掉失败步骤 ``produces`` 声明的产物、注入
            ``_feedback`` 结构化反馈后回退到 ``goto_step``。

        注意：成功一步即清 ``_feedback``（否则会污染后续所有步骤的输入）。
        """
        state: str | None = self.entry
        data: dict[str, Any] = dict(payload)
        retries = 0
        steps_run: list[str] = []
        attempt_map: dict[str, int] = {}

        while state is not None:
            # ★ v2：整次 Run 的墙钟预算。v1 只有单步 timeout_s，整条链路没有上限——
            #   六步各自 60s 再加两次 goto，单次提问最坏能跑十几分钟。
            if ctx.deadline_at is not None and time.monotonic() > ctx.deadline_at:
                await emitter.emit("run.aborted", {"reason": "run_deadline_exceeded"})
                return PipelineResult(
                    status="timeout",
                    output=data,
                    steps_run=steps_run,
                    retries=retries,
                    error_code="RUN_DEADLINE_EXCEEDED",
                    error_message="run deadline exceeded",
                )

            if ctx.cancel_token and ctx.cancel_token.cancelled:
                return PipelineResult(
                    status="aborted",
                    output=data,
                    steps_run=steps_run,
                    retries=retries,
                    error_code="ABORTED",
                    error_message=ctx.cancel_token.reason,
                )

            step = self.steps[state]
            attempt = attempt_map.get(state, 0) + 1
            attempt_map[state] = attempt
            steps_run.append(state)

            # ★ v2：访问次数上限——goto 配错成环时快速失败，而不是把 token 预算打光
            if attempt > step.max_visit:
                await emitter.emit(
                    "run.error",
                    {"code": "50014", "message": "步骤访问次数超限", "retryable": False},
                )
                return PipelineResult(
                    status="failed",
                    output=data,
                    steps_run=steps_run,
                    retries=retries,
                    error_code="STEP_VISIT_EXCEEDED",
                    error_message=f"step {state} visited {attempt} times",
                )

            await emitter.emit("agent.step.started", {"step": state, "attempt": attempt})
            await self._fire("before", state, data, ctx)

            started = time.perf_counter()
            try:
                agent = self.registry.get(step.agent_name)
                # ★ v2：单步超时真正生效（v1 声明了 timeout_s 却从未使用）
                async with asyncio.timeout(step.timeout_s):
                    out = await agent.run(data, ctx)
                data.update(out.data)
                # ★ v2：成功即清掉回退反馈，否则它会被后续所有步骤当成有效输入
                data.pop("_feedback", None)
                if out.confidence is not None:
                    data.setdefault("_confidence", {})[state] = out.confidence
                await self._fire("after", state, data, ctx)
                await emitter.emit(
                    "agent.step.finished",
                    {
                        "step": state,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "confidence": out.confidence,
                    },
                )
                state = step.next

            except TimeoutError as exc:
                # ★ v2：超时是独立语义——只有显式声明 on_timeout='goto' 的步骤才回退
                assert state is not None  # 与 while 条件一致，供类型收窄
                await self._fire("error", state, data, ctx, exc)
                await emitter.emit(
                    "agent.step.finished",
                    {
                        "step": state,
                        "status": "timeout",
                        "duration_ms": int(step.timeout_s * 1000),
                    },
                )
                if step.on_timeout == "goto" and step.goto_step and attempt <= step.max_retries:
                    retries += 1
                    for key in step.produces:
                        data.pop(key, None)
                    data["_feedback"] = {
                        "failed_step": state,
                        "error_code": "STEP_TIMEOUT",
                        "error_message": f"{state} 执行超时（{step.timeout_s}s）",
                        "attempt": attempt,
                    }
                    await emitter.emit(
                        "agent.step.retrying",
                        {"step": state, "attempt": attempt, "error": "timeout"},
                    )
                    state = step.goto_step
                    continue
                await emitter.emit(
                    "run.error", {"code": "50013", "message": "步骤执行超时", "retryable": True}
                )
                return PipelineResult(
                    status="timeout",
                    output=data,
                    steps_run=steps_run,
                    retries=retries,
                    timeout_steps=[state],
                    error_code="STEP_TIMEOUT",
                    error_message=f"step {state} timed out after {step.timeout_s}s",
                )

            except Exception as exc:  # noqa: BLE001
                assert state is not None  # 与 while 条件一致，供类型收窄
                await self._fire("error", state, data, ctx, exc)
                code, message = _classify(exc)

                # ★ 越权类失败：绝不重试（防止模型试图绕过权限）
                if code == "SQL_PERMISSION_DENIED":
                    await emitter.emit(
                        "run.error", {"code": code, "message": message, "retryable": False}
                    )
                    return PipelineResult(
                        status="failed",
                        output=data,
                        steps_run=steps_run,
                        retries=retries,
                        error_code=code,
                        error_message=message,
                    )

                if step.on_error == "retry" and attempt <= step.max_retries:
                    retries += 1
                    await emitter.emit(
                        "agent.step.retrying",
                        {"step": state, "attempt": attempt, "error": message},
                    )
                    continue  # 原地重试

                # ★ 自愈回退：带着错误信息回到上游步骤重生成
                if step.on_error == "goto" and step.goto_step and attempt <= step.max_retries:
                    retries += 1
                    # ★ v2：回退前清掉失败步骤声明的产物，避免半成品被判为有效值
                    for key in step.produces:
                        data.pop(key, None)
                    data["_feedback"] = {  # 注入给上游的结构化反馈
                        "failed_step": state,
                        "error_code": code,
                        "error_message": message,
                        "attempt": attempt,
                    }
                    await emitter.emit(
                        "sql.validation.failed",
                        {"attempt": attempt, "code": code, "message": message},
                    )
                    state = step.goto_step
                    continue

                await emitter.emit(
                    "run.error", {"code": code, "message": message, "retryable": False}
                )
                return PipelineResult(
                    status="failed",
                    output=data,
                    steps_run=steps_run,
                    retries=retries,
                    error_code=code,
                    error_message=message,
                )

        return PipelineResult(status="success", output=data, steps_run=steps_run, retries=retries)
