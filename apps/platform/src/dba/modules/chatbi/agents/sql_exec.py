"""④.5 ``SqlExecAgent``：真实执行（★ v2 新增的第七个步骤）。

对齐《设计方案 v2》§6.2 / §6.3.4 与《实现要点清单》§5.9（3.6）。

★ 与 ⑤ dry-run 护栏的区别：dry-run 只做 ``EXPLAIN``（不取数），本步骤才真正取数，
  因此超时 / 行数 / 权限三重约束在这里同时生效。

★ 唯一执行入口：本 Agent 不直接拿连接，一律调 ``QueryExecutor.execute``——
  任何绕过它的取数路径都违反红线 6。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

from ..executor import QueryExecutor

__all__ = ["SqlExecAgent", "columns_from_rows"]

logger = logging.getLogger("dba.modules.chatbi.sql_exec")

#: 主查询的数据源引用（与 spec_builder 的 DEFAULT_REF 对齐）
_PRIMARY_REF = "q1"


def columns_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """按结果行推断列定义（供大屏 ``data_sources.columns``）。"""
    if not rows:
        return []
    out: list[dict[str, str]] = []
    for name, value in rows[0].items():
        if isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, (int, float)):
            kind = "number"
        else:
            kind = "string"
        out.append({"name": str(name), "type": kind})
    return out


class SqlExecAgent:
    """真实取数（无状态）。"""

    name = "sql_exec"

    def __init__(self, executor: QueryExecutor) -> None:
        self._executor = executor

    @traced("step.sql_exec", kind="tool")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        sql = str(payload.get("sql") or "")
        scope = payload.get("_scope")
        options = payload.get("options") or {}
        max_rows = int(options.get("max_rows", 5000))

        if ctx.emitter is not None:
            await ctx.emitter.emit("sql.executing", {"sql": sql, "max_rows": max_rows})

        # 把 schema_link 阶段算出的 scope_hash 带进执行上下文（供 sql_audit 记录）。
        scope_hash = payload.get("scope_hash") or ctx.scope_hash
        effective = dataclasses.replace(ctx, scope_hash=scope_hash) if scope_hash else ctx
        rows, meta = await self._executor.execute(sql, effective, scope, max_rows=max_rows)

        # ★ 多查询：主查询之外的 ref 逐条执行（**同一条 QueryExecutor**，护栏全覆盖）。
        #   无 ``queries`` 时只返回主结果，单查询路径行为与改动前完全一致。
        query_results: dict[str, dict[str, Any]] = {
            _PRIMARY_REF: {"rows": rows, "columns": columns_from_rows(rows)}
        }
        for extra in await self._run_extra_queries(payload, effective, scope, max_rows):
            query_results.update(extra)

        if ctx.emitter is not None:
            await ctx.emitter.emit(
                "sql.executed",
                {
                    "rows_returned": meta["row_count"],
                    "exec_ms": meta["exec_ms"],
                    "scope_injected": meta["scope_injected"],
                    "truncated": meta["truncated"],  # ★ 超 max_rows 是截断，不是静默丢行
                    "query_count": len(query_results),
                },
            )
        return AgentOutput(
            data={
                "rows": rows,
                "row_count": meta["row_count"],
                "columns": columns_from_rows(rows),
                "truncated": meta["truncated"],
                "query_results": query_results,
            }
        )

    async def _run_extra_queries(
        self,
        payload: dict[str, Any],
        ctx: RunContext,
        scope: Any,
        max_rows: int,
    ) -> list[dict[str, dict[str, Any]]]:
        """执行主查询之外的附加查询；单条失败只跳过该条，不影响主链路。"""
        queries = payload.get("queries")
        if not isinstance(queries, list) or len(queries) < 2:
            return []
        out: list[dict[str, dict[str, Any]]] = []
        for item in queries:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "")
            extra_sql = str(item.get("sql") or "")
            if not ref or not extra_sql or ref == _PRIMARY_REF:
                continue
            try:
                extra_rows, _ = await self._executor.execute(
                    extra_sql, ctx, scope, max_rows=max_rows
                )
            except Exception as exc:  # noqa: BLE001 - 附加查询失败不该打挂整条流水线
                logger.warning("附加查询 ref=%s 执行失败，已跳过：%s", ref, exc)
                out.append({ref: {"rows": [], "columns": []}})
                continue
            out.append({ref: {"rows": extra_rows, "columns": columns_from_rows(extra_rows)}})
        return out
