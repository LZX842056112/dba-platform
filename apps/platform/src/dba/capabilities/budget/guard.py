"""预算守卫护栏策略（§6.5）。

对齐《设计文档 v2》§6.5 与《实现要点清单》§1.2.6.2、U10。

★ U10（照抄 v1 会怎样错）：软限动作（降级/压缩/限流）必须显式声明「允许的降级动作集合」，
且**硬熔断默认关闭**（``allow_circuit_break=False``）——否则一次误配就会把全线业务熔断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["GuardrailPolicy"]


@dataclass(frozen=True)
class GuardrailPolicy:
    """护栏配置（由 ``Settings.guardrail_policy()`` 构造）。"""

    enabled: bool = True
    allow_downgrade: bool = True
    allow_compress: bool = True
    allow_rate_limit: bool = True
    allow_circuit_break: bool = False
    exemption_priority: int = 50
    breaker_window_s: int = 60
    breaker_consecutive_windows: int = 3
    breaker_cooldown_s: int = 300
    max_downgrades_per_run: int = 2
    kill_switch: bool = False
    degrade_policy: str = "fail_open_mysql"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuardrailPolicy:
        fields = {
            "enabled",
            "allow_downgrade",
            "allow_compress",
            "allow_rate_limit",
            "allow_circuit_break",
            "exemption_priority",
            "breaker_window_s",
            "breaker_consecutive_windows",
            "breaker_cooldown_s",
            "max_downgrades_per_run",
            "kill_switch",
            "degrade_policy",
        }
        kwargs = {k: v for k, v in data.items() if k in fields}
        return cls(**kwargs)

    def allowed_soft_actions(self) -> tuple[str, ...]:
        """当前策略下**允许**执行的软限动作集合。"""
        actions: list[str] = ["ALERT"]
        if self.allow_downgrade:
            actions.append("DOWNGRADE_MODEL")
        if self.allow_compress:
            actions.append("COMPRESS_CONTEXT")
        if self.allow_rate_limit:
            actions.append("RATE_LIMIT")
        return tuple(actions)
