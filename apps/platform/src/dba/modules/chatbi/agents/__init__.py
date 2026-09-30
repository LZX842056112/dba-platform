"""ChatBI 六角色 + ``sql_exec``（共七个步骤的 Agent 实现）。

对齐《设计方案 v2》§6.2 与《实现要点清单》§5.9。

六个业务角色：``intent`` / ``schema_link`` / ``sql_gen`` / ``sql_guard`` /
``sql_exec`` / ``visual`` / ``narrator``（``sql_exec`` 为 ★ v2 新增的第七步）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from dba_runtime.registry import AgentRegistry

if TYPE_CHECKING:
    from dba_runtime.agent import Agent

from .intent import IntentAgent
from .narrator import NarratorAgent
from .schema_link import SchemaLinkAgent
from .sql_exec import SqlExecAgent, columns_from_rows
from .sql_gen import SqlGeneratorAgent, extract_sql
from .sql_guard import SqlGuardAgent
from .visual import VisualAgent

__all__ = [
    "IntentAgent",
    "SchemaLinkAgent",
    "SqlGeneratorAgent",
    "SqlGuardAgent",
    "SqlExecAgent",
    "VisualAgent",
    "NarratorAgent",
    "extract_sql",
    "columns_from_rows",
    "register_chatbi_agents",
]


def register_chatbi_agents(
    registry: AgentRegistry,
    *,
    router: Any,
    semantic: Any,
    scope_compiler: Any,
    chain: Any,
    executor: Any,
    spec_builder: Any,
    memory: Any | None = None,
    skill: Any | None = None,
    schema_vec: Any | None = None,
    embedder: Any | None = None,
) -> AgentRegistry:
    """把七个步骤的 Agent 注册进 ``AgentRegistry``（名称与步骤表严格一致）。

    ★ 这里对每个 Agent 做 ``cast("Agent", ...)``：业务方法的 ``run`` 被 ``@traced``
      装饰（内核 ``traced`` 未用 ``ParamSpec`` 保留签名，mypy 会把装饰后的 ``run``
      视为 ``(*Any, **Any) -> Awaitable[Any]``），因此无法结构化匹配 ``Agent`` 协议。
      这是**纯静态类型**的落差，运行期行为完全一致——故用 cast 显式桥接，而不是
      改动已冻结的内核装饰器。
    """
    registry.register("intent", cast("Agent", IntentAgent()))
    registry.register(
        "schema_link",
        cast(
            "Agent",
            SchemaLinkAgent(semantic, scope_compiler, schema_vec=schema_vec, embedder=embedder),
        ),
    )
    registry.register(
        "sql_gen", cast("Agent", SqlGeneratorAgent(router, memory=memory, skill=skill))
    )
    registry.register("sql_guard", cast("Agent", SqlGuardAgent(chain)))
    registry.register("sql_exec", cast("Agent", SqlExecAgent(executor)))
    registry.register("visual", cast("Agent", VisualAgent(router, spec_builder, skill=skill)))
    registry.register("narrator", cast("Agent", NarratorAgent(router)))
    return registry
