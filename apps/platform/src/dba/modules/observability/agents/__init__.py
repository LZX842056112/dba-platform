"""模块 03 · Agent 注册（四角色：collect → aggregate → anomaly → broadcast）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§2.1（``cast("Agent", ...)`` 说明）。

★ 为什么要 ``cast("Agent", ...)``：内核 ``@traced`` 装饰器会 ``functools.wraps`` 一个
``*args/**kwargs`` 包装函数，签名被擦除；mypy 无法静态确认其满足 ``Agent`` Protocol。
装饰是**必需**的（它负责 span 落账与上下文注入），因此这里显式 ``cast`` 收敛类型，
而非放弃装饰。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from dba_runtime.registry import AgentRegistry

from .aggregate import AggregateAgent
from .anomaly import AnomalyScanAgent
from .broadcast import BroadcastAgent
from .collect import CollectAgent

if TYPE_CHECKING:
    from dba_runtime import Agent

__all__ = ["register_observability_agents"]


def register_observability_agents(
    registry: AgentRegistry,
    *,
    collect: CollectAgent,
    aggregate: AggregateAgent,
    anomaly: AnomalyScanAgent,
    broadcast: BroadcastAgent,
) -> AgentRegistry:
    """把四个角色 Agent 注册进 ``AgentRegistry``。"""
    registry.register(collect.name, cast("Agent", collect))
    registry.register(aggregate.name, cast("Agent", aggregate))
    registry.register(anomaly.name, cast("Agent", anomaly))
    registry.register(broadcast.name, cast("Agent", broadcast))
    return registry
