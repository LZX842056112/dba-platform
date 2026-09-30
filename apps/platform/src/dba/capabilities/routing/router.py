"""成本感知模型路由（§6.4）。

对齐《设计文档 v2》§6.4 与《实现要点清单》U10。

★ U10（照抄 v1 会怎样错）
------------------------
v1 的「降级」只改 ``model`` 字符串，不改 ``max_tokens``、也不记录 ``downgrade_from``：
于是 ① 降级后仍按高档模型的长输出上限申请配额，预留金额虚高；
② 埋点里看不出「这次是降级来的」，成本归因失真。
v2 要求：每次路由产出**完整** ``ModelRoute``（provider/model/档位/max_tokens/是否降级/来源），
且降级必须在**允许动作集合**内（见 ``GuardrailPolicy.allowed_soft_actions``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["ModelSpec", "ModelRoute", "ModelRouter", "RouteStrategy"]

RouteStrategy = Literal["quality", "balanced", "cheap"]


@dataclass(frozen=True)
class ModelSpec:
    """一个可选模型档位。"""

    tier: str
    provider: str
    model: str
    max_output_tokens: int
    #: 相对成本提示（仅用于排序，真实成本以 price_book 为准）
    cost_rank: int

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class ModelRoute:
    """一次路由决策。"""

    provider: str
    model: str
    tier: str
    max_output_tokens: int
    downgraded: bool
    downgrade_from: str | None
    strategy: RouteStrategy
    reason: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier,
            "max_output_tokens": self.max_output_tokens,
            "downgraded": self.downgraded,
            "downgrade_from": self.downgrade_from,
            "strategy": self.strategy,
            "reason": self.reason,
        }


def _default_ladder() -> list[ModelSpec]:
    """默认降级阶梯（从高到低）。真实档位由配置/DB 覆盖。"""
    return [
        ModelSpec("premium", "openai", "gpt-4o", 4096, 10),
        ModelSpec("standard", "openai", "gpt-4o-mini", 2048, 3),
        ModelSpec("economy", "openai", "gpt-4o-mini", 1024, 1),
    ]


class ModelRouter:
    """成本/质量权衡的模型路由器。"""

    def __init__(
        self,
        ladder: list[ModelSpec] | None = None,
        *,
        default_strategy: RouteStrategy = "balanced",
        stream_hint: bool = True,
    ) -> None:
        self._ladder = sorted(ladder or _default_ladder(), key=lambda s: s.cost_rank, reverse=True)
        if not self._ladder:
            raise ValueError("模型阶梯不能为空")
        self._default_strategy = default_strategy
        self._stream_hint = stream_hint

    def decide(
        self,
        *,
        strategy: RouteStrategy | None = None,
        budget_decision: str = "allow",
        allow_downgrade: bool = True,
        max_downgrades: int = 2,
        current: ModelSpec | None = None,
        reason: str = "",
    ) -> ModelRoute:
        """决定本次调用用哪个模型。

        :param strategy: 期望档位（``quality``/``balanced``/``cheap``）
        :param budget_decision: 预算决策（``allow``/``soft``/``hard``）
        :param allow_downgrade: 是否允许降级（受护栏策略约束）
        """
        chosen_strategy: RouteStrategy = strategy or self._default_strategy
        spec = self._spec_for(chosen_strategy)

        # 预算软限 → 在允许范围内降级（每次降一档，最多 max_downgrades 次）
        if budget_decision == "soft" and allow_downgrade:
            spec = self.downgrade(spec, steps=min(1, max(0, max_downgrades)))
            return self._to_route(
                spec,
                chosen_strategy,
                downgraded=True,
                downgrade_from=(current.key if current else self._spec_for(chosen_strategy).key),
                reason=reason or "budget_soft_limit_downgrade",
            )

        base = current or self._spec_for(chosen_strategy)
        return self._to_route(
            spec,
            chosen_strategy,
            downgraded=current is not None and spec.cost_rank < base.cost_rank,
            downgrade_from=None,
            reason=reason or "default_route",
        )

    def downgrade(self, spec: ModelSpec, *, steps: int = 1) -> ModelSpec:
        """按成本降一档（找不到更便宜的就停在当前档）。"""
        cheaper = [s for s in self._ladder if s.cost_rank < spec.cost_rank]
        if not cheaper:
            return spec
        cheaper.sort(key=lambda s: s.cost_rank, reverse=True)
        idx = min(max(0, steps) - 1, len(cheaper) - 1)
        return cheaper[idx]

    def ladder(self) -> list[ModelSpec]:
        return list(self._ladder)

    def _spec_for(self, strategy: RouteStrategy) -> ModelSpec:
        ordered = sorted(self._ladder, key=lambda s: s.cost_rank, reverse=True)
        if strategy == "quality":
            return ordered[0]
        if strategy == "cheap":
            return ordered[-1]
        return ordered[len(ordered) // 2]

    @staticmethod
    def _to_route(
        spec: ModelSpec,
        strategy: RouteStrategy,
        *,
        downgraded: bool,
        downgrade_from: str | None,
        reason: str,
    ) -> ModelRoute:
        return ModelRoute(
            provider=spec.provider,
            model=spec.model,
            tier=spec.tier,
            max_output_tokens=spec.max_output_tokens,
            downgraded=downgraded,
            downgrade_from=downgrade_from,
            strategy=strategy,
            reason=reason,
        )
