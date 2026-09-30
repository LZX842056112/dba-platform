"""模块 10 · 四角色流水线（meter → attribute → guard → optimize）。

对齐《设计方案 v2》§2.2 / §6.5 与《实现要点清单》§3.24。

与观测流水线一样是**线性批处理**：每步失败原地重试 1 次。因为 FinOps 也是旁路治理，
一次统计失败不该让整批优化建议无产出。
"""

from __future__ import annotations

from dba_runtime import Pipeline, Step
from dba_runtime.registry import AgentRegistry

__all__ = ["FINOPS_STEPS", "build_finops_pipeline"]

#: 四角色步骤表
FINOPS_STEPS: list[Step] = [
    Step(
        name="meter",
        agent_name="meter",
        next="attribute",
        timeout_s=60,
        on_error="retry",
        max_retries=1,
        produces=("cost_summary",),
    ),
    Step(
        name="attribute",
        agent_name="attribute",
        next="guard",
        timeout_s=60,
        on_error="retry",
        max_retries=1,
        produces=("attribution",),
    ),
    Step(
        name="guard",
        agent_name="budget_guard",
        next="optimize",
        timeout_s=30,
        on_error="retry",
        max_retries=1,
        produces=("budget_decision",),
    ),
    Step(
        name="optimize",
        agent_name="optimize",
        next=None,
        timeout_s=30,
        on_error="retry",
        max_retries=1,
        produces=("recommendations",),
    ),
]


def build_finops_pipeline(registry: AgentRegistry) -> Pipeline:
    """构建 FinOps 四角色流水线。"""
    return Pipeline(
        name="finops",
        steps=list(FINOPS_STEPS),
        entry="meter",
        registry=registry,
    )
