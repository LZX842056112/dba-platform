"""事件信封与发射器（内核 L2）。

对齐《设计方案 v2》§6.1.2 + 《实现要点清单》§5.2 / §6.4。

★ U4 修正：v2 §6.1.2 的 ``EventType`` Literal 只列了 18 个，**缺 ``run.aborted`` 与
``sql.executing``**；但 §6.1.3 的 Pipeline 实际 ``emit("run.aborted")``、§9.3 事件表也列了
``sql.executing`` / ``run.aborted``。本模块补全为 **20 个**（以此为冻结版本）。
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

__all__ = [
    "EventType",
    "EventEnvelope",
    "EventEmitter",
    "InMemoryEmitter",
    "EVENT_TYPE_COUNT",
]

# ★ U4：20 个事件类型（v2 正文缺 run.aborted / sql.executing，此处已补全）
EventType = Literal[
    "run.started",
    "run.finished",
    "run.error",
    "run.aborted",  # ★ v2 新增
    "agent.step.started",
    "agent.step.delta",
    "agent.step.finished",
    "agent.step.retrying",
    "sql.generated",
    "sql.validation.failed",
    "sql.retry.resolved",
    "sql.executing",  # ★ v2 新增
    "sql.executed",
    "dashboard.spec.delta",
    "dashboard.spec.ready",
    "narration.delta",
    "budget.warning",
    "budget.downgraded",
    "budget.blocked",
    "heartbeat",
]

#: 冻结版本的事件个数（供测试断言，防止后续批次误删）
EVENT_TYPE_COUNT = 20


class EventEnvelope(BaseModel):
    """所有流式事件的统一信封。前端按 event 分派，按 seq 去重与续传。"""

    event: EventType = Field(description="事件类型（EventType 之一）")
    seq: int = Field(description="同一 trace 内单调递增；断线续传用 Last-Event-ID 对齐")
    trace_id: str
    ts: int = Field(description="毫秒时间戳")
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def build(
        cls, event: EventType, seq: int, trace_id: str, data: dict[str, Any] | None = None
    ) -> EventEnvelope:
        """便捷构造：自动填 ``ts``（毫秒墙钟）。"""
        return cls(
            event=event,
            seq=seq,
            trace_id=trace_id,
            ts=int(time.time() * 1000),
            data=data or {},
        )


class EventEmitter(Protocol):
    """Pipeline 与 Agent 通过它向外发事件，不关心底层是 SSE 还是 WebSocket。"""

    async def emit(self, event: EventType, data: dict[str, Any]) -> None: ...


class InMemoryEmitter:
    """内存事件收集器（测试 / 本地调试用）。

    ★ 非 SSE writer：真正的 SSE writer 属于 L1，负责写 ``id: {seq}``、心跳与终态收尾。
    本类只做「收集 + 自动递增 seq」，便于内核单测断言事件序列。
    """

    def __init__(self, trace_id: str = "0" * 32) -> None:
        self.trace_id = trace_id
        self.events: list[EventEnvelope] = []
        self._seq = 0

    async def emit(self, event: EventType, data: dict[str, Any]) -> None:
        self._seq += 1
        self.events.append(EventEnvelope.build(event, self._seq, self.trace_id, data))

    def names(self) -> list[str]:
        """返回已发出的事件名序列（便于断言）。"""
        return [e.event for e in self.events]
