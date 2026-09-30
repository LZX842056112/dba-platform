"""② ``SchemaLinkAgent``：语义映射 + **行级权限第一道防线**。

对齐《设计方案 v2》§6.2 / §6.3.3（防线一）与《实现要点清单》§5.9。

职责
----
1. 抽候选业务字段（本实现以语义层检索为主，规则兜底）；
2. 向量召回 ``dba_sem_metric_vec`` 得表列与 join 路径（可选，降级）；
3. ★ 读 ``row_scope_rule`` 编译 ``scope``（受保护表 + 权限列 + 允许值域）；
4. ★ 组装 prompt：口径 + 字段映射 + **权限边界硬约束** ——
   把「可访问表清单 + 权限值域」写进 system，让模型「生来就知道边界」，
   从源头减少越权 SQL 的产生概率。

★ 为什么第一道不能省：只靠事后注入，模型会生成大量需要改写的 SQL，
  注入逻辑要处理的形态变多、出错概率上升。第一道让输出「本来就合规」。
"""

from __future__ import annotations

from typing import Any, cast

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

from ..guard.row_scope import CompiledScope, RowScopeCompiler
from ..prompts import load_prompt

__all__ = ["SchemaLinkAgent"]


class SchemaLinkAgent:
    """语义映射 + 权限边界（无状态）。"""

    name = "schema_link"

    def __init__(
        self,
        semantic: Any,
        scope: RowScopeCompiler,
        schema_vec: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        self._semantic = semantic
        self._scope = scope
        self._vector = schema_vec
        self._embedder = embedder

    @traced("step.schema_link", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        question = str(payload.get("question") or "")
        biz_line_id = ctx.biz_line_id

        # 3) ★ 权限边界：编译 scope（含 rule_version，参与 scope_hash）
        scope = await self._scope.compile(
            role_ids=list(payload.get("_role_ids") or []),
            user_vars=dict(payload.get("_user_vars") or {}),
        )
        scope_hash = RowScopeCompiler.scope_hash(scope) if scope is not None else None

        # 1) + 2) 指标口径与字段映射
        metrics = await self._metrics(question, biz_line_id)
        fields = await self._fields()
        accessible = sorted(scope.protected_tables()) if scope is not None else []

        scope_desc = self._scope_desc(scope)
        prompt = self._build_prompt(question, metrics, fields, scope_desc)

        return AgentOutput(
            data={
                "question": question,
                "metrics": metrics,
                "fields": fields,
                "tables": accessible,
                "joins": self._joins(fields),
                "scope": scope_desc,
                "_scope": scope,
                "scope_hash": scope_hash,
                "schema_prompt": prompt,
            }
        )

    async def _metrics(self, question: str, biz_line_id: int | None) -> list[dict[str, Any]]:
        if self._semantic is None:
            return []
        try:
            rows = await self._semantic.resolve_metrics(question, biz_line_id)
        except Exception:  # noqa: BLE001 - 语义层可降级：缺口径仍可生成 SQL
            return []
        return [
            {
                "metric_code": m.metric_code,
                "metric_name": m.metric_name,
                "caliber": m.caliber_desc,
                "sql_expr": m.sql_expr,
                "unit": m.unit,
            }
            for m in rows
        ]

    async def _fields(self) -> dict[str, list[dict[str, Any]]]:
        if self._semantic is None:
            return {}
        try:
            return cast("dict[str, list[dict[str, Any]]]", await self._semantic.field_map())
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _joins(fields: dict[str, list[dict[str, Any]]]) -> list[str]:
        joins: list[str] = []
        for rows in fields.values():
            for row in rows:
                join_path = row.get("join_path")
                if isinstance(join_path, str) and join_path and join_path not in joins:
                    joins.append(join_path)
        return joins

    @staticmethod
    def _scope_desc(scope: CompiledScope | None) -> dict[str, Any]:
        if scope is None:
            return {}
        return {
            table: {"column": scope.column_for(table), "values": scope.values_for(table)}
            for table in sorted(scope.protected_tables())
        }

    @staticmethod
    def _build_prompt(
        question: str,
        metrics: list[dict[str, Any]],
        fields: dict[str, list[dict[str, Any]]],
        scope_desc: dict[str, Any],
    ) -> str:
        template = load_prompt("schema_link") or "把业务说法映射到指标口径与物理表列。"
        metric_lines = "\n".join(
            f"- {m['metric_code']}（{m['metric_name']}，口径：{m['caliber']}，unit={m['unit']}）"
            for m in metrics
        )
        field_lines = "\n".join(
            f"- {logical} → {row.get('physical_table')}.{row.get('physical_column')}"
            for logical, rows in fields.items()
            for row in rows
        )
        scope_lines = "\n".join(
            f"- {table}.{info['column']} IN {info['values']}" for table, info in scope_desc.items()
        )
        return (
            f"{template}\n\n## 指标口径\n{metric_lines or '（无）'}\n\n"
            f"## 字段映射\n{field_lines or '（无）'}\n\n"
            f"## 权限边界（硬约束，不可放宽）\n{scope_lines or '（无行级限制）'}\n\n"
            f"## 用户问题\n{question}"
        )
