"""语义服务（指标口径 / 字段映射 / 词典 / 行级权限规则 → scope）。

对齐《设计文档 v2》§5.2.1 / §5.2.2 / §6.6 与《实现要点清单》§6.6、P0-10。

★ 行级权限规则的**取值**会随管理员改动而变（如可见区域从「华东」改成「全国」）。
  因此本服务把「规则集 + 规则版本（最大 updated_at）」一起产出，
  交给 ``capabilities.memory.build_scope_hash`` 生成 ``scope_hash``（§6.6）。
  只 hash 谓词文本、不带 rule_version 是 v1 的坑（改了值 hash 不变 → 缓存继续越权命中）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from dba.capabilities.memory.scope import build_scope_hash
from dba.storage.milvus.collections import COL_SEM_METRIC

__all__ = ["MetricDef", "SemanticService", "ScopeBuild", "build_scope_from_rules"]

logger = logging.getLogger("dba.capabilities.semantics")


@dataclass
class MetricDef:
    """一条指标口径。"""

    metric_code: str
    metric_name: str
    caliber_desc: str
    sql_expr: str
    unit: str
    biz_line_id: int | None = None
    version: int = 1
    default_dims: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MetricDef:
        dims = row.get("default_dims") or []
        if isinstance(dims, str):
            dims = json.loads(dims)
        return cls(
            metric_code=str(row["metric_code"]),
            metric_name=str(row["metric_name"]),
            caliber_desc=str(row.get("caliber_desc", "")),
            sql_expr=str(row.get("sql_expr", "")),
            unit=str(row.get("unit", "CNY")),
            biz_line_id=row.get("biz_line_id"),
            version=int(row.get("version", 1)),
            default_dims=list(dims),
        )


@dataclass
class ScopeBuild:
    """一次权限范围构建的结果（含 rule_version）。"""

    accessible_tables: tuple[str, ...]
    predicates: tuple[str, ...]
    rule_version: str | None
    scope_hash: str


def _render_predicate(rule: dict[str, Any], ctx_vars: dict[str, Any]) -> str | None:
    """把一条 ``row_scope_rule`` 渲染成规范谓词文本（如 ``region IN ('east','west')``）。"""
    table = str(rule.get("physical_table", ""))
    column = str(rule.get("scope_column", ""))
    operator = str(rule.get("operator", "IN"))
    value_type = str(rule.get("value_type", "STATIC"))
    if not table or not column:
        return None

    if value_type == "CTX_VAR":
        var = str(rule.get("ctx_var") or "")
        raw = ctx_vars.get(var)
        if raw is None:
            # ★ 权限变量缺失必须**显式拒绝**（返回空值域），绝不能因此放行全表
            return f"{table}.{column} {operator} ()"
        values = raw if isinstance(raw, (list, tuple)) else [raw]
    else:
        raw_json = rule.get("value_json")
        values = (
            raw_json
            if isinstance(raw_json, (list, tuple))
            else ([raw_json] if raw_json is not None else [])
        )

    if operator in ("IN", "NOT IN") or operator.upper() in ("IN", "NOT IN"):
        rendered = ", ".join(_sql_literal(v) for v in values)
        return f"{table}.{column} {operator} ({rendered})"
    if operator == "BETWEEN" and len(values) >= 2:
        return f"{table}.{column} BETWEEN {_sql_literal(values[0])} AND {_sql_literal(values[1])}"
    if values:
        return f"{table}.{column} {operator} {_sql_literal(values[0])}"
    return f"{table}.{column} {operator} ()"


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_scope_from_rules(rule_rows: list[dict[str, Any]], rule_version: str | None) -> ScopeBuild:
    """由 ``row_scope_rule`` 行集合构建权限范围 + ``scope_hash``。"""
    tables: list[str] = []
    predicates: list[str] = []
    for rule in rule_rows:
        table = str(rule.get("physical_table", ""))
        if table and table not in tables:
            tables.append(table)
        pred = _render_predicate(rule, ctx_vars={})
        if pred:
            predicates.append(pred)
    scope_hash = build_scope_hash(tables, predicates, rule_version)
    return ScopeBuild(
        accessible_tables=tuple(sorted(tables)),
        predicates=tuple(sorted(predicates)),
        rule_version=rule_version,
        scope_hash=scope_hash,
    )


class SemanticService:
    """指标解析 / 字段映射 / 词典查询 / 权限范围（只依赖 Protocol）。"""

    def __init__(
        self,
        *,
        metric_repo: Any,
        field_repo: Any,
        dict_repo: Any,
        role_repo: Any | None = None,
        scope_rule_repo: Any | None = None,
        vector_repo: Any | None = None,
    ) -> None:
        self._metrics = metric_repo
        self._fields = field_repo
        self._dict = dict_repo
        self._roles = role_repo
        self._scope_rules = scope_rule_repo
        self._vector = vector_repo

    async def resolve_metrics(
        self, keywords: str, biz_line_id: int | None, *, limit: int = 8
    ) -> list[MetricDef]:
        """按关键词解析指标口径（注册表优先，向量召回补足）。"""
        rows = await self._metrics.search(keywords, biz_line_id, limit=limit)
        out = [MetricDef.from_row(r) for r in rows]
        if out:
            return out
        if self._vector is None:
            return []
        try:
            from dba.capabilities.embedding.client import HashEmbedder  # noqa: PLC0415

            vector = HashEmbedder().embed_one(keywords)
            expr = f"biz_line_id == {biz_line_id}" if biz_line_id is not None else ""
            hits = await self._vector.search(
                COL_SEM_METRIC, vector, expr=expr, limit=limit, output_fields=["metric_code"]
            )
        except Exception:  # noqa: BLE001
            logger.warning("指标向量召回失败", exc_info=True)
            return []
        codes = [str(h.get("metric_code")) for h in hits if h.get("metric_code")]
        if not codes:
            return []
        return [MetricDef.from_row(r) for r in await self._metrics.by_codes(codes, biz_line_id)]

    async def field_map(self) -> dict[str, list[dict[str, Any]]]:
        """逻辑字段 → 物理映射（供 SQL 生成阶段使用）。"""
        out: dict[str, list[dict[str, Any]]] = {}
        for row in await self._fields.all_mappings():
            out.setdefault(str(row["logical_field"]), []).append(dict(row))
        return out

    async def lookup_dict(self, term: str, biz_line_id: int | None) -> list[dict[str, Any]]:
        return [dict(r) for r in await self._dict.search(term, biz_line_id)]

    async def build_scope(
        self, user_id: int, *, ctx_vars: dict[str, Any] | None = None
    ) -> ScopeBuild:
        """构建用户权限范围（含 ``rule_version`` → ``scope_hash``）。"""
        if self._roles is None or self._scope_rules is None:
            return ScopeBuild((), (), None, build_scope_hash((), (), None))
        role_ids = await self._roles.role_ids_of_user(user_id)
        rules = await self._scope_rules.rules_for_roles(role_ids)
        version = await self._scope_rules.rule_version(role_ids)
        build = build_scope_from_rules(list(rules), version)
        if ctx_vars:
            predicates = [_render_predicate(rule, ctx_vars) or "" for rule in rules]
            predicates = [p for p in predicates if p]
            scope_hash = build_scope_hash(build.accessible_tables, predicates, version)
            return ScopeBuild(
                build.accessible_tables, tuple(sorted(predicates)), version, scope_hash
            )
        return build
