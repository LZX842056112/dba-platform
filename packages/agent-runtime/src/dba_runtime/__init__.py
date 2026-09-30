"""``dba_runtime`` —— 可复用运行时内核（L2）。

设计约束（《设计方案 v2》§4.4 / §6.6）：本包**故意保持极少依赖**——
只依赖 ``pydantic`` + ``opentelemetry-api`` + Python 标准库。
不装任何数据库驱动、不装 FastAPI、不装 ``opentelemetry-sdk``。

因此它能被任意项目复用，也保证了「内核不知道存储在哪儿」；
内核单测可以用 ``fake`` 实现完整跑通，不需要任何外部组件。

★ B1 是本项目的**接口冻结节点**：本文件导出的 Protocol 一经交付，
后续批次（B2~B6）**只实现不改造**（见《实现要点清单》§4.3）。
"""

from __future__ import annotations

from .agent import Agent, AgentOutput
from .budget import (
    BudgetDecision,
    BudgetGuard,
    BudgetUsage,
    Reservation,
    SettlementResult,
)
from .context import (
    CancelToken,
    RunContext,
    ctx,
    ctx_or_none,
    new_span_id,
    new_trace_id,
    new_ulid,
    reset_current,
    set_current,
)
from .errors import (
    BudgetExceededError,
    DbaError,
    DryRunFailedError,
    PermissionDeniedError,
    RunDeadlineExceededError,
    ScopeEmptyError,
    SqlGuardError,
    StepTimeoutError,
    StepVisitExceededError,
    StorageUnavailableError,
    TelemetryNotBoundError,
    TransientSqlError,
    classify,
)
from .events import EVENT_TYPE_COUNT, EventEmitter, EventEnvelope, EventType, InMemoryEmitter
from .memory import CacheHit, MemoryClient, RecallResult
from .pipeline import Pipeline, PipelineResult, Step, StepHook
from .registry import AgentRegistry, ToolRegistry
from .router import LLMResult, ModelChoice, ModelRouter
from .skills import SkillService, SkillSpec, SkillStats
from .telemetry import (
    CostNormalizerProto,
    LLMCallRecord,
    MeteringService,
    NormalizedCost,
    SpanRecord,
    ToolCallRecord,
    bind_metering,
    drain_queue,
    dropped_total,
    enqueue,
    flush_queue,
    is_bound,
    metered,
    queue_depth,
    traced,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # context
    "RunContext",
    "CancelToken",
    "set_current",
    "reset_current",
    "ctx",
    "ctx_or_none",
    "new_trace_id",
    "new_span_id",
    "new_ulid",
    # events
    "EventType",
    "EventEnvelope",
    "EventEmitter",
    "InMemoryEmitter",
    "EVENT_TYPE_COUNT",
    # agent / pipeline
    "Agent",
    "AgentOutput",
    "Pipeline",
    "PipelineResult",
    "Step",
    "StepHook",
    # registry
    "AgentRegistry",
    "ToolRegistry",
    # errors
    "DbaError",
    "SqlGuardError",
    "BudgetExceededError",
    "StepTimeoutError",
    "StepVisitExceededError",
    "RunDeadlineExceededError",
    "PermissionDeniedError",
    "ScopeEmptyError",
    "StorageUnavailableError",
    "TelemetryNotBoundError",
    "TransientSqlError",
    "DryRunFailedError",
    "classify",
    # memory / skills
    "MemoryClient",
    "RecallResult",
    "CacheHit",
    "SkillSpec",
    "SkillStats",
    "SkillService",
    # router / budget
    "ModelChoice",
    "LLMResult",
    "ModelRouter",
    "BudgetDecision",
    "Reservation",
    "SettlementResult",
    "BudgetUsage",
    "BudgetGuard",
    # telemetry
    "MeteringService",
    "CostNormalizerProto",
    "NormalizedCost",
    "LLMCallRecord",
    "ToolCallRecord",
    "SpanRecord",
    "bind_metering",
    "enqueue",
    "drain_queue",
    "flush_queue",
    "traced",
    "metered",
    "dropped_total",
    "queue_depth",
    "is_bound",
]
