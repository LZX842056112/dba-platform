"""模块 10 · FinOps（Agent 成本治理平台 · M5）。

对齐《设计方案 v2》§6.5 / §7.5 / §5.9 与《实现要点清单》§2.3、§3.24~3.28、B5。

组成
----
* **四角色流水线**（``pipeline.FINOPS_STEPS``）：meter → attribute → guard → optimize；
* **护栏与预算守卫**（``guardrail``，★ U10：降级走 ``with_downgrade``）；
* **死循环检测**（``loop_detector``）；
* **复用率-成本曲线**（``cost_curve``，带对照组）；
* **只读服务**（``service.FinopsService``）。

★ 成本归一化（``CostNormalizer``）在 **L4** ``capabilities/cost``（§5.9 归属修正），
  本模块只消费它，不重复实现（N3 一致性）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dba_runtime.registry import AgentRegistry

from .agents import register_finops_agents
from .agents.attribute import AttributeAgent, CostAttributor
from .agents.guard import BudgetGuardAgent
from .agents.meter import MeterAgent
from .agents.optimize import OptimizeAgent
from .cost_curve import CostCurveAnalyzer
from .guardrail import GuardrailPolicy
from .loop_detector import InMemoryLoopCounter, LoopCounter, LoopDetector
from .pipeline import FINOPS_STEPS, build_finops_pipeline
from .service import FinopsService

__all__ = [
    "FinopsBundle",
    "build_finops",
    "FINOPS_STEPS",
    "FinopsService",
    "GuardrailPolicy",
    "BudgetGuardAgent",
    "CostCurveAnalyzer",
    "LoopDetector",
]


@dataclass
class FinopsBundle:
    """模块 10 的装配产物（供 L1 接入层取用）。"""

    registry: AgentRegistry
    pipeline: Any
    service: FinopsService
    guard: BudgetGuardAgent
    attributor: CostAttributor
    curve: CostCurveAnalyzer
    loop_detector: LoopDetector
    policy: GuardrailPolicy


def build_finops(
    *,
    observability: Any = None,
    budget: Any = None,
    reco_repo: Any = None,
    detail_source: Any = None,
    curve_source: Any = None,
    coverage_source: Any = None,
    loop_counter: LoopCounter | None = None,
    alert_repo: Any = None,
    cost_normalizer: Any = None,
    policy: GuardrailPolicy | None = None,
    alerts: Any = None,
) -> FinopsBundle:
    """装配模块 10（归因 → 曲线 → 护栏 → 四 Agent → 流水线 → 只读服务）。"""
    effective_policy = policy or GuardrailPolicy()
    attributor = CostAttributor(detail_source=detail_source, cost_normalizer=cost_normalizer)
    curve = CostCurveAnalyzer(curve_source)
    loop_detector = LoopDetector(loop_counter or InMemoryLoopCounter(), alert_repo=alert_repo)
    guard = BudgetGuardAgent(guard=budget, policy=effective_policy, alerts=alerts)

    registry = register_finops_agents(
        AgentRegistry(),
        meter=MeterAgent(observability=observability),
        attribute=AttributeAgent(attributor),
        guard=guard,
        optimize=OptimizeAgent(reco_repo=reco_repo),
    )
    pipeline = build_finops_pipeline(registry)

    service = FinopsService(
        observability=observability,
        attributor=attributor,
        curve=curve,
        reco_repo=reco_repo,
        coverage_source=coverage_source,
        budget=budget,
        policy=effective_policy,
    )
    return FinopsBundle(
        registry=registry,
        pipeline=pipeline,
        service=service,
        guard=guard,
        attributor=attributor,
        curve=curve,
        loop_detector=loop_detector,
        policy=effective_policy,
    )
