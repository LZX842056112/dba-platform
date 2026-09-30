"""模块 01 · ChatBI（对话式 BI）。

对齐《设计方案 v2》§6.2 / §6.3 / §6.6 与《实现要点清单》§2.3。

本模块由四部分组成：

* **七步流水线**（``pipeline.CHATBI_STEPS``）：``intent → schema_link → sql_gen →
  sql_guard → sql_exec → visual → narrator``；
* **五道 SQL 护栏**（``guard/``）：readonly → dialect → row_scope → row_scope_verify →
  limit → dry_run；
* **唯一执行入口**（``executor.QueryExecutor``，红线 6）；
* **大屏 JSON 组装**（``spec_builder.DashboardSpecBuilder``）。

★ 红线 1：本模块**禁止**被其他 L3 模块 import（Ruff ``banned-api`` 强制）；
  模块内部文件互相引用使用相对 import（绝对路径 ``dba.modules.chatbi.*`` 会被 TID251 命中）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dba_runtime.registry import AgentRegistry

from .agents import register_chatbi_agents
from .executor import QueryExecutor
from .guard import (
    FieldMappingJoinResolver,
    ReadOnlyConnection,
    RowScopeCompiler,
    SqlGuardChain,
    build_guard_chain,
)
from .guard.base import GuardResult, SqlGuardError
from .pipeline import CHATBI_STEPS, build_chatbi_pipeline
from .spec_builder import DashboardSpecBuilder

__all__ = [
    "ChatbiBundle",
    "build_chatbi",
    "CHATBI_STEPS",
    "QueryExecutor",
    "DashboardSpecBuilder",
    "SqlGuardChain",
    "SqlGuardError",
    "GuardResult",
    "RowScopeCompiler",
    "FieldMappingJoinResolver",
    "ReadOnlyConnection",
]


@dataclass
class ChatbiBundle:
    """ChatBI 模块的装配产物（供 L1 接入层直接取用）。"""

    registry: AgentRegistry
    pipeline: Any
    guard_chain: SqlGuardChain
    executor: QueryExecutor
    spec_builder: DashboardSpecBuilder
    scope_compiler: RowScopeCompiler


def build_chatbi(
    *,
    audit: Any,
    scope_rule_repo: Any,
    field_mapping_repo: Any,
    readonly_pool: Any = None,
    router: Any = None,
    semantic: Any = None,
    memory: Any | None = None,
    skill: Any | None = None,
    object_store: Any | None = None,
    allowed_tables: set[str] | None = None,
    extra_functions: set[str] | None = None,
    table_loader: Any | None = None,
    max_rows: int = 5000,
    timeout_s: float = 30.0,
    inline_threshold: int = 500,
    bucket: str = "dba-query-results",
) -> ChatbiBundle:
    """装配 ChatBI 模块（护栏链 → 执行器 → 七步 Agent → 流水线）。"""
    readonly_conn = ReadOnlyConnection(readonly_pool)
    chain = build_guard_chain(
        audit=audit,
        scope_rule_repo=scope_rule_repo,
        field_mapping_repo=field_mapping_repo,
        readonly_conn=readonly_conn,
        allowed_tables=allowed_tables,
        extra_functions=extra_functions,
        table_loader=table_loader,
        max_rows=max_rows,
        timeout_s=timeout_s,
    )
    executor = QueryExecutor(chain, readonly_pool, audit)
    spec_builder = DashboardSpecBuilder(
        object_store=object_store, bucket=bucket, inline_threshold=inline_threshold
    )
    registry = register_chatbi_agents(
        AgentRegistry(),
        router=router,
        semantic=semantic,
        scope_compiler=RowScopeCompiler(scope_rule_repo),
        chain=chain,
        executor=executor,
        spec_builder=spec_builder,
        memory=memory,
        skill=skill,
    )
    pipeline = build_chatbi_pipeline(registry)
    return ChatbiBundle(
        registry=registry,
        pipeline=pipeline,
        guard_chain=chain,
        executor=executor,
        spec_builder=spec_builder,
        scope_compiler=RowScopeCompiler(scope_rule_repo),
    )
