"""Agent 与 AgentOutput（内核 L2）。

对齐《设计方案 v2》§6.1.3 / 《实现要点清单》§5.3。

★ 硬约束：Agent 实现必须**无状态**——所有状态放 RunContext 或 payload。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .context import RunContext

__all__ = ["AgentOutput", "Agent"]


class AgentOutput(BaseModel):
    """Agent 的统一输出。

    ``data`` 会被 Pipeline ``data.update(out.data)`` 合并进流水线数据包；
    ``confidence`` 供下游判断是否需要人工确认；``meta`` 携带非语义化的辅助信息。
    """

    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None  # 0~1，供下游判断是否需人工确认
    meta: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Agent(Protocol):
    """一个专职 Agent。实现必须是无状态的——所有状态放 RunContext 或 payload。"""

    name: str

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput: ...
