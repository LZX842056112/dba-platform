"""内核单测共享夹具（无任何数据库 / 网络依赖）。"""

from __future__ import annotations

from typing import Any

import pytest
from dba_runtime.telemetry import LLMCallRecord, SpanRecord, ToolCallRecord


class CollectingMetering:
    """内存计量服务：把 ``@traced`` / ``@metered`` 落账的记录收集起来供断言。"""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []
        self.llms: list[LLMCallRecord] = []
        self.tools: list[ToolCallRecord] = []
        self.dlq: list[tuple[str, dict[str, Any]]] = []

    async def record_llm(self, rec: LLMCallRecord) -> None:
        self.llms.append(rec)

    async def record_tool(self, rec: ToolCallRecord) -> None:
        self.tools.append(rec)

    async def record_span(self, rec: SpanRecord) -> None:
        self.spans.append(rec)

    async def record_dlq(self, kind: str, rec: dict[str, Any]) -> None:
        self.dlq.append((kind, rec))


@pytest.fixture
def metering() -> CollectingMetering:
    return CollectingMetering()
