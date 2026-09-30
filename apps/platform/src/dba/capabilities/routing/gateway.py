"""``ModelGateway`` —— **内核 ``ModelRouter`` Protocol 的实现**（L4 能力层）。

对齐《设计方案 v2》§6.1.5 与《实现要点清单》§4.4（能力 4.4）、§5.5。

★ 职责边界（与 ``router.ModelRouter`` 的分工）
--------------------------------------------
* ``router.ModelRouter``：**纯决策**（策略表 → 档位 / 降级链 / max_tokens），无副作用；
* ``ModelGateway``：在决策之上叠加 **预算预留 → 调用 → 计量 → 结算/释放** 的完整顺序，
  并把结果包装成内核 ``ModelChoice`` / ``LLMResult``。

★ §6.1.5 内部顺序（每一步都不可省）
----------------------------------
① ``reservation_id``（ULID，本次调用唯一）
② ``BudgetGuard.reserve``（Redis Lua 原子预留，落到 MySQL 权威账本）
③ 若决策为 ``soft`` → 走降级（``with_downgrade`` 语义由调用方持有；此处记录事件）
④ 调用 SDK；失败 / 超时 / 取消都要走 ``except``
⑤ ``CostNormalizer.normalize`` + ``MeteringService.record_llm``（经 outbox，由 ``@metered`` 完成）
⑥ ``BudgetGuard.settle``（幂等）
⑦ 失败路径：``BudgetGuard.release``（幂等）

★ ``@metered("llm")`` 负责 ⑤：它用**全局绑定**的成本归一化器（``bind_metering`` 时注入），
  因此 P0-3（计费输入 = prompt − cached）在此路径上自动生效。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from dba_runtime.context import RunContext
from dba_runtime.errors import BudgetExceededError
from dba_runtime.router import LLMResult, ModelChoice
from dba_runtime.telemetry import NormalizedCost, metered

from .router import ModelRouter, RouteStrategy

__all__ = ["ModelGateway", "DEFAULT_TASK_STRATEGY"]

logger = logging.getLogger("dba.capabilities.routing")

#: 任务 → 路由策略（未列出走 balanced）
DEFAULT_TASK_STRATEGY: dict[str, RouteStrategy] = {
    "intent": "cheap",
    "schema_link": "cheap",
    "sql_gen": "quality",
    "visual_layout": "balanced",
    "narration": "balanced",
}

#: 质量档位 → 路由策略
_QUALITY_STRATEGY: dict[str, RouteStrategy] = {
    "eco": "cheap",
    "std": "balanced",
    "max": "quality",
}


class ModelGateway:
    """内核 ``ModelRouter`` Protocol 的实现（route / chat / stream）。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        llm: Any,
        budget: Any | None = None,
        cost: Any | None = None,
        task_strategy: dict[str, RouteStrategy] | None = None,
        budget_period: str = "MONTH",
        default_timeout_s: float = 60.0,
    ) -> None:
        self._router = router
        self._llm = llm
        self._budget = budget
        self._cost = cost
        self._task_strategy = dict(DEFAULT_TASK_STRATEGY)
        if task_strategy:
            self._task_strategy.update(task_strategy)
        self._budget_period = budget_period
        self._timeout_s = default_timeout_s

    # ── 路由（纯决策包装）──────────────────────────────────────────
    async def route(
        self,
        *,
        task: str,
        ctx: RunContext,
        quality: Literal["eco", "std", "max"] | None = None,
    ) -> ModelChoice:
        strategy = self._task_strategy.get(
            task, _QUALITY_STRATEGY.get(quality or ctx.quality, "balanced")
        )
        decision = self._router.decide(
            strategy=strategy, budget_decision="allow", allow_downgrade=False
        )
        return ModelChoice(
            provider=decision.provider,
            model=decision.model,
            max_tokens=decision.max_output_tokens,
            temperature=0.0,
            degraded_from=decision.downgrade_from,
            route_policy=strategy,
        )

    # ── 调用（含预算 + 计量）───────────────────────────────────────
    @metered("llm")
    async def _complete(
        self, messages: list[dict[str, Any]], choice: ModelChoice, ctx: RunContext
    ) -> LLMResult:
        _ = ctx
        return cast(
            "LLMResult", await self._llm.complete(messages, choice, timeout_s=self._timeout_s)
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        ctx: RunContext,
        **kwargs: Any,
    ) -> LLMResult:
        """调用 LLM：预留 → 调用 → 结算 / 释放（计量由 ``@metered`` 完成）。"""
        _ = kwargs
        hold = await self._reserve(ctx, choice)
        try:
            result = await self._complete(messages, choice, ctx)
        except Exception:
            await self._release(hold)
            raise
        await self._settle(hold, result, choice)
        return cast("LLMResult", result)

    def stream(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        ctx: RunContext,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式返回（``NarratorAgent`` 逐 chunk emit ``narration.delta``）。

        ★ 流式路径不做 token 记账：多数供应商在流结束前不返回 usage，
        为「假装有账」而记 0 反而会污染成本曲线（P0-3 的同类问题）。
        流式调用的成本由供应商账单在 ``reconcile`` 中回填。
        """
        _ = ctx, kwargs
        return cast("AsyncIterator[str]", self._llm.stream(messages, choice))

    # ── 预算三段式 ────────────────────────────────────────────────
    async def _reserve(self, ctx: RunContext, choice: ModelChoice) -> Any | None:
        if self._budget is None:
            return None
        budget = await self._resolve_budget(ctx)
        if budget is None:
            return None
        estimated = self._budget.estimate(
            provider=choice.provider,
            model=choice.model,
            prompt_tokens=0,
            max_completion_tokens=choice.max_tokens,
        )
        decision = await self._budget.reserve(
            budget, estimated_micro_usd=estimated, trace_id=ctx.trace_id
        )
        if not decision.allowed:
            await self._emit(ctx, "budget.blocked", decision.as_dict)
            raise BudgetExceededError(
                "预算硬上限阻断本次调用", code="42901", detail=decision.as_dict
            )
        if decision.decision == "soft":
            await self._emit(ctx, "budget.warning", decision.as_dict)
        return (budget, decision.reservation_id)

    async def _settle(self, hold: Any | None, result: LLMResult, choice: ModelChoice) -> None:
        if hold is None or self._budget is None:
            return
        budget, reservation_id = hold
        if reservation_id is None:
            return
        actual = self._cost_of(result, choice)
        try:
            await self._budget.settle(
                budget,
                reservation_id=reservation_id,
                actual_micro_usd=actual,
                usage_source=result.usage_source,
            )
        except Exception:  # noqa: BLE001 - 结算失败不阻断主链路（权威账本由对账兜底）
            logger.warning("预算结算失败（已记日志，交由对账兜底）", exc_info=True)

    async def _release(self, hold: Any | None) -> None:
        if hold is None or self._budget is None:
            return
        budget, reservation_id = hold
        if reservation_id is None:
            return
        try:
            await self._budget.release(budget, reservation_id=reservation_id)
        except Exception:  # noqa: BLE001
            logger.warning("预算释放失败（预留将到期自动回收）", exc_info=True)

    async def _resolve_budget(self, ctx: RunContext) -> Any | None:
        budget = self._budget
        if budget is None:
            return None
        scopes: list[tuple[str, str]] = []
        for key in ctx.budget_keys:
            scope_type, _, scope_id = key.partition(":")
            if scope_type and scope_id:
                scopes.append((scope_type.upper(), scope_id))
        if not scopes:
            return None
        return await budget.resolve_chain(scopes, self._budget_period)

    def _cost_of(self, result: LLMResult, choice: ModelChoice) -> int:
        if self._cost is None:
            return 0
        cost: NormalizedCost = self._cost.normalize(
            {
                "model": choice.model,
                "provider": choice.provider,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cached_tokens": result.cached_tokens,
                "usage_source": result.usage_source,
            }
        )
        return cost.total_micro_usd

    @staticmethod
    async def _emit(ctx: RunContext, event: Any, data: dict[str, Any]) -> None:
        if ctx.emitter is not None:
            await ctx.emitter.emit(event, data)
