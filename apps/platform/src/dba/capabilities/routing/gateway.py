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
        """按任务类型 / 质量档位选模型（**纯决策，无副作用**）。

        输入：``task``（如 ``sql_gen``）、``ctx``、可选 ``quality`` 覆盖。
        输出：``ModelChoice``（provider / model / max_tokens / 路由策略）。
        注意：此方法**不预留预算、不计费**；预算三段式在 ``chat`` 里完成。
        """
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
        """真正发起一次 LLM 调用；``@metered("llm")`` 装饰器负责写成本 / token 埋点。

        ★ 计费的「输入 token = prompt − cached」修正由全局成本归一化器在 ``@metered``
          内部完成（P0-3），此处不重复处理。
        """
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
        """预算预留（**调用前**，check-and-reserve 前置）。

        输入：``ctx``（含 ``budget_keys`` 作用域链）、``choice``（模型与 max_tokens）。
        输出：``(budget, reservation_id)`` 句柄；未配置预算 / 作用域为空时返回 ``None``
              （表示本次调用不记账，后续 settle/release 直接跳过）。
        注意：硬上限阻断时抛 ``BudgetExceededError``（code 42901），
              软限只发 ``budget.warning`` 事件、不阻断（依据 §6.5 默认策略）。
        """
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
        """结算预算：把预留换成**实际花费**（``reserved -= 预估``、``consumed += 实际``）。

        注意：结算失败**不阻断**主链路——权威账本由 worker 对账兜底，不把计量失败
              升级成业务失败（与埋点 DLQ 同一设计原则）。
        """
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
        """释放预留：调用失败 / 超时 / 取消时归还额度（幂等；重复释放不改数）。

        注意：释放失败也不抛错——预留带 ``expires_at``，超期由定时任务标记 ``EXPIRED`` 回收。
        """
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
        """把 ``ctx.budget_keys`` 解析为具体预算对象（**从具体到宽泛**选生效者）。

        输入：``ctx.budget_keys`` 形如 ``("AGENT:ag_x", "BIZ_LINE:12", "GLOBAL:*")``。
        输出：链上第一个命中的预算；作用域为空 / 无命中 → ``None``。
        """
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
        """把一次调用结果归一化为 micro_usd 整数成本（无价格表时返回 0）。

        ★ 这里的 0 是「未计价」而非「免费」：``CostNormalizer`` 会标 ``unknown``，
          覆盖率看板据此暴露「有多少调用没价可查」，不会把未知静默当 0 计入总和。
        """
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
        """发 run 级事件（无 emitter 时静默跳过，不报错）。"""
        if ctx.emitter is not None:
            await ctx.emitter.emit(event, data)
