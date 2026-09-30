"""模型路由（内核 L2）。

对齐《设计方案 v2》§6.1.5 / 《实现要点清单》§5.5。

★ U5：``ModelRouter`` Protocol **缺 ``stream`` 方法**（``NarratorAgent`` 要用），此处补：
``def stream(self, messages, choice, ctx, **kwargs) -> AsyncIterator[str]``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from .context import RunContext

__all__ = ["ModelChoice", "LLMResult", "ModelRouter"]


class ModelChoice(BaseModel):
    """一次 LLM 调用的模型选择（路由产物）。"""

    provider: str
    model: str
    max_tokens: int
    temperature: float = 0.0
    degraded_from: str | None = None  # ★ 若被预算守卫降级，记录原定模型
    reason: str | None = None  # 降级原因
    route_policy: str | None = None


class LLMResult(BaseModel):
    """LLM 调用结果。``usage_dict()`` 供 ``@metered`` 装饰器直接落账。"""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    latency_ms: int
    ttft_ms: int | None = None
    finish_reason: str | None = None
    usage_source: Literal["measured", "self_reported"] = "measured"  # ★ v2
    provider: str | None = None  # 由适配器填充，便于 llm_call.provider 落库

    def usage_dict(self) -> dict[str, Any]:
        """返回可并入 ``LLMCallRecord`` 的 usage 字段集合。"""
        return {
            "model": self.model,
            "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "ttft_ms": self.ttft_ms,
            "usage_source": self.usage_source,
        }


@runtime_checkable
class ModelRouter(Protocol):
    """策略表驱动的路由 + 降级链。实现放在 L4 ``capabilities/routing``。"""

    async def route(
        self,
        *,
        task: str,
        ctx: RunContext,
        quality: Literal["eco", "std", "max"] | None = None,
    ) -> ModelChoice:
        """选择模型。策略表按 task 名配置候选链，quality 决定取哪一档。

        预算守卫通过 ``ctx.quality`` 影响结果。
        """
        ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        ctx: RunContext,
        **kwargs: Any,
    ) -> LLMResult:
        """真正调用。业务代码不应直接调用 SDK。

        ★ v2 内部顺序（每一步都不可省，§6.1.5）：
          ① ``reservation_id = new_ulid()``（本次调用唯一，重试复用）
          ② ``BudgetGuard.reserve(ctx, est, reservation_id)`` —— Redis Lua 原子预留
          ③ ``ctx = ctx.with_downgrade(...)``（若决策为 DOWNGRADE_MODEL）
          ④ 调用 SDK；失败 / 超时 / 取消都要走 ``finally``
          ⑤ ``CostNormalizer.normalize(...)`` → ``MeteringService.record_llm(...)``（经 outbox）
          ⑥ ``BudgetGuard.settle(reservation_id, actual)`` —— 幂等
          ⑦ 失败路径：``BudgetGuard.release(reservation_id, reason)`` —— 幂等
        """
        ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        ctx: RunContext,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """★ U5：流式返回（``NarratorAgent`` 逐 chunk emit ``narration.delta``）。"""
        ...
