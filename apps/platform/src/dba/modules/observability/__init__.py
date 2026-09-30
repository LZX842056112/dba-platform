"""模块 03 · Observability（Agent 可观测性平台 · M4）。

对齐《设计方案 v2》§6.4 / §7.4 / §5.9 与《实现要点清单》§2.3、§3.18~3.23、B5。

组成
----
* **四角色流水线**（``pipeline.OBSERVABILITY_STEPS``）：collect → aggregate → anomaly → broadcast；
* **无阈值异常检测**（``anomaly.AnomalyDetector``，EWMA+MAD）与**静默失败检测**；
* **三条价值指标**（``metrics``）：技能复用率 / 死技能 / 记忆命中率；
* **指标 rollup**（``rollup.RollupService``，幂等，写 ``metric_daily``）；
* **拓扑聚合**（``topology.TopologyBuilder``）与 **OTLP 接入**（``otel_ingest``）；
* **只读服务**（``service.ObservabilityService``）——owner 对外读接口。

★ 红线 1：本模块**禁止**被其他 L3 模块 import（Ruff ``banned-api`` 强制）；
  模块内部文件互相引用使用相对 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dba_runtime.registry import AgentRegistry

from .agents import register_observability_agents
from .agents.aggregate import AggregateAgent
from .agents.anomaly import AnomalyScanAgent
from .agents.broadcast import BroadcastAgent, BroadcastGateway
from .agents.collect import CollectAgent
from .anomaly import (
    SELF_MODULES,
    AnomalyDetector,
    MetricDailySeriesProvider,
    SilentFailureDetector,
    SilentFailureRecord,
    Window,
)
from .metrics import MemoryHitRateCalculator, SkillMetricsCalculator
from .otel_ingest import OtelSpanIngester
from .pipeline import OBSERVABILITY_STEPS, build_observability_pipeline
from .rollup import RollupService
from .service import ObservabilityService
from .topology import TopologyBuilder

__all__ = [
    "ObservabilityBundle",
    "build_observability",
    "OBSERVABILITY_STEPS",
    "ObservabilityService",
    "AnomalyDetector",
    "SilentFailureDetector",
    "RollupService",
    "TopologyBuilder",
    "OtelSpanIngester",
    "SELF_MODULES",
]


@dataclass
class ObservabilityBundle:
    """模块 03 的装配产物（供 L1 接入层与 worker 取用）。"""

    registry: AgentRegistry
    pipeline: Any
    service: ObservabilityService
    rollup: RollupService
    detector: AnomalyDetector
    silent_detector: SilentFailureDetector
    otel: OtelSpanIngester
    topology: TopologyBuilder
    skill_metrics: SkillMetricsCalculator
    memory_metrics: MemoryHitRateCalculator


def build_observability(
    *,
    alert_repo: Any = None,
    metric_repo: Any = None,
    agent_repo: Any = None,
    run_repo: Any = None,
    report_repo: Any = None,
    skills: Any = None,
    memory: Any = None,
    metering: Any = None,
    gateway: BroadcastGateway | None = None,
) -> ObservabilityBundle:
    """装配模块 03（检测器 → 指标 → rollup → 四 Agent → 流水线 → 只读服务）。"""
    skill_metrics = SkillMetricsCalculator(skills)
    memory_metrics = MemoryHitRateCalculator(memory=memory, metric_repo=metric_repo)

    detector = AnomalyDetector(loader=MetricDailySeriesProvider(metric_repo))
    silent_detector = SilentFailureDetector(loader=_make_silent_loader(run_repo))

    rollup = RollupService(
        run_repo=run_repo,
        metric_repo=metric_repo,
        skill_metrics=skill_metrics,
        memory_metrics=memory_metrics,
        exclude_modules=SELF_MODULES,
    )
    topology = TopologyBuilder(metering=metering, run_repo=run_repo)
    otel = OtelSpanIngester(metering=metering)

    registry = register_observability_agents(
        AgentRegistry(),
        collect=CollectAgent(run_repo=run_repo, agent_repo=agent_repo),
        aggregate=AggregateAgent(rollup),
        anomaly=AnomalyScanAgent(
            detector=detector,
            silent_detector=silent_detector,
            alert_repo=alert_repo,
            report_repo=report_repo,
        ),
        broadcast=BroadcastAgent(gateway=gateway),
    )
    pipeline = build_observability_pipeline(registry)

    service = ObservabilityService(
        alert_repo=alert_repo,
        metric_repo=metric_repo,
        agent_repo=agent_repo,
        run_repo=run_repo,
        report_repo=report_repo,
        skill_metrics=skill_metrics,
        memory_metrics=memory_metrics,
        topology=topology,
    )
    return ObservabilityBundle(
        registry=registry,
        pipeline=pipeline,
        service=service,
        rollup=rollup,
        detector=detector,
        silent_detector=silent_detector,
        otel=otel,
        topology=topology,
        skill_metrics=skill_metrics,
        memory_metrics=memory_metrics,
    )


def _make_silent_loader(run_repo: Any) -> Any:
    """构造静默失败明细 loader（读 ``run``，排除平台自身模块，P1-4）。"""

    async def _load(window: Window) -> list[SilentFailureRecord]:
        if run_repo is None:
            return []
        try:
            runs = await run_repo.list_runs(
                {
                    "since": window.since,
                    "until": window.until,
                    "limit": 10000,
                },
                limit=10000,
            )
        except Exception:  # noqa: BLE001 - 静默失败检测可降级
            return []
        out: list[SilentFailureRecord] = []
        for run in runs:
            module = str(run.get("module") or "")
            if module in SELF_MODULES:
                continue
            out.append(
                SilentFailureRecord(
                    trace_id=str(run.get("trace_id") or ""),
                    module=module,
                    status=str(run.get("status") or ""),
                    tokens=int(run.get("tokens_in") or 0) + int(run.get("tokens_out") or 0),
                    tool_calls=int(run.get("tool_calls") or 0),
                    latency_ms=int(run.get("latency_ms") or 0),
                    p5_latency_ms=None,  # P5 基准需 ES 明细，本批次标注 TODO
                    agent_uid=run.get("agent_uid"),
                    biz_line_id=run.get("biz_line_id"),
                )
            )
        return out

    return _load


def _default_window(hours: int = 24) -> tuple[datetime, datetime]:
    """预览用默认窗口（近 N 小时，UTC naive）。"""
    until = datetime.now(UTC).replace(tzinfo=None)
    return until - timedelta(hours=hours), until
