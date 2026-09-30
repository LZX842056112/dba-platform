"""工具 Protocol（内核 L2，[P1]）。

对齐《设计方案 v2》§6.1.3 与《实现要点清单》§5.3、U7、U8。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from dba_runtime.context import RunContext

__all__ = ["ToolResult", "Tool"]


class ToolResult(BaseModel):
    """工具执行结果。"""

    ok: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    """★ U7：``Tool`` Protocol 为 ``name: str`` + ``async run(args, ctx) -> ToolResult``。"""

    name: str

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult: ...
