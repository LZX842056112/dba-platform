"""模块 03 · 四角色流水线（collect → aggregate → anomaly → broadcast）。

对齐《设计方案 v2》§2.2 / §6.4 与《实现要点清单》§3.18。

设计取舍
--------
与 ChatBI 的七步自愈流水线不同，观测流水线是**线性批处理**：每一步失败都只做
「原地重试 1 次」然后**继续**（``on_error='fail'`` 语义下 Pipeline 会终止，因此这里
用 ``retry``）。因为观测是旁路：**任何一步失败都不该让整批扫描无产出**——
上游采集失败仍应尝试聚合已存数据，异常扫描失败不应影响播报已发现的事件。
"""

from __future__ import annotations

from dba_runtime import Pipeline, Step
from dba_runtime.registry import AgentRegistry

__all__ = ["OBSERVABILITY_STEPS", "build_observability_pipeline"]

#: 四角色步骤表（timeout 后原地重试一次，避免观测链路被单点失败拖垮）
OBSERVABILITY_STEPS: list[Step] = [
    Step(
        name="collect",
        agent_name="collect",
        next="aggregate",
        timeout_s=60,
        on_error="retry",
        max_retries=1,
        produces=("collected",),
    ),
    Step(
        name="aggregate",
        agent_name="aggregate",
        next="anomaly",
        timeout_s=120,
        on_error="retry",
        max_retries=1,
        produces=("rollup",),
    ),
    Step(
        name="anomaly",
        agent_name="anomaly",
        next="broadcast",
        timeout_s=120,
        on_error="retry",
        max_retries=1,
        produces=("alerts", "alert_count"),
    ),
    Step(
        name="broadcast",
        agent_name="broadcast",
        next=None,
        timeout_s=30,
        on_error="retry",
        max_retries=1,
        produces=("broadcast",),
    ),
]


def build_observability_pipeline(registry: AgentRegistry) -> Pipeline:
    """构建观测四角色流水线。"""
    return Pipeline(
        name="observability",
        steps=list(OBSERVABILITY_STEPS),
        entry="collect",
        registry=registry,
    )
