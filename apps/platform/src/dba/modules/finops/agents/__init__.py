"""模块 10 · Agent 注册（四角色：meter → attribute → guard → optimize）。

对齐《设计方案 v2》§6.5 与《实现要点清单》§2.1、§3.25。

★ ``BudgetGuardAgent`` 定义在 ``guardrail.py``（护栏与执行动作同源，便于单测 U10），
本包 ``agents/guard.py`` 仅**再导出**，保证两个文件清单（3.25 / 3.26）都能命中同一实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from dba_runtime.registry import AgentRegistry

from .attribute import AttributeAgent
from .guard import BudgetGuardAgent
from .meter import MeterAgent
from .optimize import OptimizeAgent

if TYPE_CHECKING:
    from dba_runtime import Agent

__all__ = [
    "BudgetGuardAgent",
    "register_finops_agents",
]


def register_finops_agents(
    registry: AgentRegistry,
    *,
    meter: MeterAgent,
    attribute: AttributeAgent,
    guard: BudgetGuardAgent,
    optimize: OptimizeAgent,
) -> AgentRegistry:
    """把四角色 Agent 注册进 ``AgentRegistry``。"""
    registry.register(meter.name, cast("Agent", meter))
    registry.register(attribute.name, cast("Agent", attribute))
    registry.register(guard.name, cast("Agent", guard))
    registry.register(optimize.name, cast("Agent", optimize))
    return registry
