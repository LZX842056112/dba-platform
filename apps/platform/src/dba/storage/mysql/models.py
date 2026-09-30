"""MySQL 21 张表 ORM 模型（严格对齐《设计文档 v2》§5.2 DDL）。

★ 表数说明：§5.2.1(5) + §5.2.2(3) + §5.2.3(5) + §5.2.4(3) + §5.2.5(5) = **21 张**。
  《实现要点清单》§1/§6.15 写的「20 张」是 v1 口径（v2 新增 ``budget_reservation`` 后为 21）。

★ 刻意的反范式：``run`` / ``llm_call`` / ``tool_call`` 之间**只留 trace_id 软关联，不建外键**
  （§5.1）——高写入路径建外键会拖慢吞吐并妨碍分区裁剪。

模型仅供 **metadata / autogenerate / 类型化 Core 查询** 使用；
真正的建表 DDL 由 ``migrations/versions/0001_initial_schema.py`` 以 §5.2 原文落地。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "BizLine",
    "AuthUser",
    "AuthRole",
    "AuthUserRole",
    "RowScopeRule",
    "SemMetric",
    "SemFieldMapping",
    "SemDictEntry",
    "AppAgent",
    "Run",
    "LlmCall",
    "ToolCall",
    "PriceBook",
    "Budget",
    "BudgetUsage",
    "BudgetReservation",
    "SkillRegistry",
    "SkillUsage",
    "MetricDaily",
    "AlertEvent",
    "SqlAudit",
    "Outbox",
]

_U = mysql.BIGINT(unsigned=True)
_DT3 = mysql.DATETIME(fsp=3)
_NOW3 = sa.text("CURRENT_TIMESTAMP(3)")
_TABLE_KW: dict[str, Any] = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Base(DeclarativeBase):
    """声明式基类。"""

    __table_args__ = _TABLE_KW


# ── §5.2.1 组织与权限组 ────────────────────────────────────────────────
class BizLine(Base):
    __tablename__ = "biz_line"
    __table_args__ = {**_TABLE_KW, "comment": "业务线"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("biz_line.id"), nullable=True)
    manager_user_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    status: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)


class AuthUser(Base):
    __tablename__ = "auth_user"
    __table_args__ = {**_TABLE_KW, "comment": "用户"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    biz_line_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("biz_line.id"), nullable=True)
    status: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)


class AuthRole(Base):
    __tablename__ = "auth_role"
    __table_args__ = {**_TABLE_KW, "comment": "角色"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class AuthUserRole(Base):
    __tablename__ = "auth_user_role"
    __table_args__ = {**_TABLE_KW, "comment": "用户-角色关联"}

    user_id: Mapped[int] = mapped_column(_U, sa.ForeignKey("auth_user.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(_U, sa.ForeignKey("auth_role.id"), primary_key=True)


class RowScopeRule(Base):
    __tablename__ = "row_scope_rule"
    __table_args__ = {**_TABLE_KW, "comment": "行级权限规则（下沉到 SQL 生成阶段的硬约束）"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(_U, sa.ForeignKey("auth_role.id"), nullable=False)
    physical_table: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    scope_column: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    operator: Mapped[str] = mapped_column(
        sa.Enum("IN", "=", "NOT IN", "LIKE", "BETWEEN", name="rsr_operator"),
        nullable=False,
        server_default=sa.text("'IN'"),
    )
    value_type: Mapped[str] = mapped_column(
        sa.Enum("STATIC", "CTX_VAR", name="rsr_value_type"),
        nullable=False,
        server_default=sa.text("'STATIC'"),
    )
    value_json: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    ctx_var: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("100"))
    enabled: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3
    )


# ── §5.2.2 语义层组 ────────────────────────────────────────────────────
class SemMetric(Base):
    __tablename__ = "sem_metric"
    __table_args__ = {**_TABLE_KW, "comment": "指标口径定义（权威源）"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    metric_code: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    metric_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    biz_line_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("biz_line.id"), nullable=True)
    caliber_desc: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sql_expr: Mapped[str] = mapped_column(sa.Text, nullable=False)
    unit: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, server_default=sa.text("'CNY'")
    )
    include_tax: Mapped[int | None] = mapped_column(sa.SmallInteger, nullable=True)
    region_scope: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    default_dims: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    status: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )
    created_by: Mapped[int | None] = mapped_column(_U, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3
    )


class SemFieldMapping(Base):
    __tablename__ = "sem_field_mapping"
    __table_args__ = {**_TABLE_KW, "comment": "逻辑字段 → 物理表列映射"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    metric_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("sem_metric.id"), nullable=True)
    logical_field: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    physical_table: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    physical_column: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    join_path: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    is_dimension: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("0")
    )
    sample_values: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class SemDictEntry(Base):
    __tablename__ = "sem_dict_entry"
    __table_args__ = {**_TABLE_KW, "comment": "口径词典（结构化部分）"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    synonyms: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    metric_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("sem_metric.id"), nullable=True)
    biz_line_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("biz_line.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3
    )


# ── §5.2.3 Run 埋点与成本明细组 ────────────────────────────────────────
class AppAgent(Base):
    __tablename__ = "app_agent"
    __table_args__ = {**_TABLE_KW, "comment": "Agent 注册中心"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    agent_uid: Mapped[str] = mapped_column(sa.CHAR(36), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    biz_line_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("biz_line.id"), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    runtime_type: Mapped[str] = mapped_column(
        sa.Enum("internal_pipeline", "external_sdk", "otel_agent", name="agent_runtime_type"),
        nullable=False,
    )
    version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    entrypoint: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.Enum("active", "idle", "retired", name="agent_status"),
        nullable=False,
        server_default=sa.text("'active'"),
    )
    tags: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    registered_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)


class Run(Base):
    __tablename__ = "run"
    __table_args__ = {
        **_TABLE_KW,
        "comment": "Run 元数据（按月分区）",
        "mysql_partition_by": (
            "RANGE COLUMNS(started_at) ("
            "PARTITION p202609 VALUES LESS THAN ('2026-10-01'),"
            "PARTITION p202610 VALUES LESS THAN ('2026-11-01'),"
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        ),
    }

    trace_id: Mapped[str] = mapped_column(sa.CHAR(32), primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(_DT3, primary_key=True)
    module: Mapped[str] = mapped_column(
        sa.Enum("chatbi", "observability", "finops", "system", name="run_module"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    biz_line_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    agent_uid: Mapped[str | None] = mapped_column(sa.CHAR(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    parent_trace: Mapped[str | None] = mapped_column(sa.CHAR(32), nullable=True)
    scope_hash: Mapped[str | None] = mapped_column(sa.CHAR(32), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.Enum("running", "success", "failed", "timeout", "aborted", name="run_status"),
        nullable=False,
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    span_count: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
    )
    llm_calls: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
    )
    tool_calls: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
    )
    tokens_in: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    tokens_out: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    cached_tokens: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    cost_micro_usd: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class LlmCall(Base):
    __tablename__ = "llm_call"
    __table_args__ = {**_TABLE_KW, "comment": "LLM 调用明细"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(sa.CHAR(32), nullable=False)
    span_id: Mapped[str] = mapped_column(sa.CHAR(16), nullable=False)
    biz_line_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    agent_uid: Mapped[str | None] = mapped_column(sa.CHAR(36), nullable=True)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    route_policy: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    downgrade_from: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
    )
    cached_tokens: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")
    )
    price_book_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    cost_micro_usd: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    latency_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    cache_hit: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("0")
    )
    status: Mapped[str] = mapped_column(
        sa.Enum("ok", "error", "timeout", name="llm_status"), nullable=False
    )
    usage_source: Mapped[str] = mapped_column(
        sa.Enum("measured", "self_reported", name="usage_source"),
        nullable=False,
        server_default=sa.text("'measured'"),
    )
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class ToolCall(Base):
    __tablename__ = "tool_call"
    __table_args__ = {**_TABLE_KW, "comment": "工具调用明细"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(sa.CHAR(32), nullable=False)
    span_id: Mapped[str] = mapped_column(sa.CHAR(16), nullable=False)
    biz_line_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    agent_uid: Mapped[str | None] = mapped_column(sa.CHAR(36), nullable=True)
    tool_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    args_hash: Mapped[str | None] = mapped_column(sa.CHAR(64), nullable=True)
    status: Mapped[str] = mapped_column(sa.Enum("ok", "error", name="tool_status"), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    cost_micro_usd: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class PriceBook(Base):
    __tablename__ = "price_book"
    __table_args__ = {**_TABLE_KW, "comment": "归一化价格表"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    billing_unit: Mapped[str] = mapped_column(
        sa.Enum("PER_1K_TOKEN", "PER_CALL", "PER_SECOND", name="billing_unit"), nullable=False
    )
    input_price_micro_usd: Mapped[int] = mapped_column(
        _U, nullable=False, server_default=sa.text("0")
    )
    output_price_micro_usd: Mapped[int] = mapped_column(
        _U, nullable=False, server_default=sa.text("0")
    )
    cache_read_price_micro_usd: Mapped[int] = mapped_column(
        _U, nullable=False, server_default=sa.text("0")
    )
    cache_write_price_micro_usd: Mapped[int] = mapped_column(
        _U, nullable=False, server_default=sa.text("0")
    )
    currency: Mapped[str] = mapped_column(
        sa.CHAR(3), nullable=False, server_default=sa.text("'USD'")
    )
    fx_rate_to_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(18, 8), nullable=False, server_default=sa.text("1.0")
    )
    tier_json: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    effective_from: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False)
    effective_to: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


# ── §5.2.4 预算与配额组 ────────────────────────────────────────────────
class Budget(Base):
    __tablename__ = "budget"
    __table_args__ = {**_TABLE_KW, "comment": "分级预算"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(
        sa.Enum("GLOBAL", "BIZ_LINE", "AGENT", "TEAM", name="budget_scope_type"), nullable=False
    )
    scope_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    period: Mapped[str] = mapped_column(
        sa.Enum("DAY", "WEEK", "MONTH", name="budget_period"), nullable=False
    )
    amount_micro_usd: Mapped[int] = mapped_column(_U, nullable=False)
    soft_limit_pct: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("80")
    )
    # ★ P0-5：默认 90（有效区间 1~99；=100 视为禁用 HARD 档，Lua 里 hard<limit 才可能命中）
    hard_limit_pct: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("90")
    )
    soft_action: Mapped[str] = mapped_column(
        sa.Enum(
            "ALERT", "DOWNGRADE_MODEL", "COMPRESS_CONTEXT", "RATE_LIMIT", name="budget_soft_action"
        ),
        nullable=False,
        server_default=sa.text("'ALERT'"),
    )
    hard_action: Mapped[str] = mapped_column(
        sa.Enum("BLOCK", "CIRCUIT_BREAK", name="budget_hard_action"),
        nullable=False,
        server_default=sa.text("'BLOCK'"),
    )
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("100"))
    timezone: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, server_default=sa.text("'Asia/Shanghai'")
    )
    enabled: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3
    )


class BudgetUsage(Base):
    __tablename__ = "budget_usage"
    __table_args__ = {**_TABLE_KW, "comment": "预算用量（权威账本；快路径在 Redis）"}

    budget_id: Mapped[int] = mapped_column(_U, sa.ForeignKey("budget.id"), primary_key=True)
    period_start: Mapped[dt.date] = mapped_column(sa.Date, primary_key=True)
    consumed_micro_usd: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    reserved_micro_usd: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    call_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    breaker_state: Mapped[str] = mapped_column(
        sa.Enum("CLOSED", "HALF_OPEN", "OPEN", name="breaker_state"),
        nullable=False,
        server_default=sa.text("'CLOSED'"),
    )
    breaker_opened_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3
    )


class BudgetReservation(Base):
    __tablename__ = "budget_reservation"
    __table_args__ = {**_TABLE_KW, "comment": "预算预留明细（幂等结算的依据）"}

    reservation_id: Mapped[str] = mapped_column(sa.CHAR(26), primary_key=True)
    budget_id: Mapped[int] = mapped_column(_U, sa.ForeignKey("budget.id"), nullable=False)
    period_start: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(sa.CHAR(32), nullable=True)
    estimated_micro_usd: Mapped[int] = mapped_column(_U, nullable=False)
    actual_micro_usd: Mapped[int | None] = mapped_column(_U, nullable=True)
    state: Mapped[str] = mapped_column(
        sa.Enum("RESERVED", "SETTLED", "RELEASED", "EXPIRED", name="rsv_state"),
        nullable=False,
        server_default=sa.text("'RESERVED'"),
    )
    decision: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False)
    settled_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


# ── §5.2.5 技能、聚合与审计组 ──────────────────────────────────────────
class SkillRegistry(Base):
    __tablename__ = "skill_registry"
    __table_args__ = {**_TABLE_KW, "comment": "技能注册表（元数据 + 步骤 + 参数三层）"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    skill_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    biz_line_id: Mapped[int | None] = mapped_column(_U, sa.ForeignKey("biz_line.id"), nullable=True)
    meta_json: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    steps_json: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    params_schema: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    parent_skill: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    usage_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    reuse_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    success_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    fail_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)
    is_dead: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("0")
    )
    status: Mapped[str] = mapped_column(
        sa.Enum("draft", "active", "archived", name="skill_status"),
        nullable=False,
        server_default=sa.text("'draft'"),
    )
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class SkillUsage(Base):
    __tablename__ = "skill_usage"
    __table_args__ = {**_TABLE_KW, "comment": "技能使用记录"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(_U, sa.ForeignKey("skill_registry.id"), nullable=False)
    trace_id: Mapped[str] = mapped_column(sa.CHAR(32), nullable=False)
    biz_line_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    is_reuse: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("0")
    )
    tokens_saved_est: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    baseline_cost_micro_usd: Mapped[int] = mapped_column(
        _U, nullable=False, server_default=sa.text("0")
    )
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class MetricDaily(Base):
    __tablename__ = "metric_daily"
    __table_args__ = {
        **_TABLE_KW,
        "comment": "每日聚合指标（按月分区）",
        "mysql_partition_by": (
            "RANGE COLUMNS(stat_date) ("
            "PARTITION p202609 VALUES LESS THAN ('2026-10-01'),"
            "PARTITION p202610 VALUES LESS THAN ('2026-11-01'),"
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        ),
    }

    stat_date: Mapped[dt.date] = mapped_column(sa.Date, primary_key=True)
    # ★ P1-1：哨兵 0（不是 -1），与 Milvus 分区键哨兵保持一致（§6.8）
    biz_line_id: Mapped[int] = mapped_column(_U, primary_key=True, server_default=sa.text("0"))
    agent_uid: Mapped[str] = mapped_column(
        sa.CHAR(36), primary_key=True, server_default=sa.text("''")
    )
    model: Mapped[str] = mapped_column(
        sa.String(64), primary_key=True, server_default=sa.text("''")
    )
    run_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    success_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    fail_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    timeout_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    silent_fail_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    tokens_in: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    tokens_out: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    cached_tokens: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    cost_micro_usd: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    avg_latency_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    p50_latency_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    p95_latency_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    cache_hit_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    downgrade_count: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))
    skill_total: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    skill_used: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    skill_reuse_rate: Mapped[Decimal] = mapped_column(
        sa.Numeric(6, 4), nullable=False, server_default=sa.text("0")
    )
    dead_skill_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    mem_lookup: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    mem_hit: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    mem_hit_rate: Mapped[Decimal] = mapped_column(
        sa.Numeric(6, 4), nullable=False, server_default=sa.text("0")
    )
    tokens_saved_est: Mapped[int] = mapped_column(_U, nullable=False, server_default=sa.text("0"))


class AlertEvent(Base):
    __tablename__ = "alert_event"
    __table_args__ = {**_TABLE_KW, "comment": "异常事件"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    detected_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    severity: Mapped[str] = mapped_column(
        sa.Enum("info", "warn", "critical", name="alert_severity"),
        nullable=False,
        server_default=sa.text("'warn'"),
    )
    category: Mapped[str] = mapped_column(
        sa.Enum(
            "cost_spike",
            "fail_rate_up",
            "skill_rot",
            "silent_failure",
            "loop_suspect",
            name="alert_category",
        ),
        nullable=False,
    )
    scope_type: Mapped[str] = mapped_column(
        sa.Enum("BIZ_LINE", "AGENT", "GLOBAL", name="alert_scope_type"), nullable=False
    )
    scope_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    metric: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    observed_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 6), nullable=True)
    baseline_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 6), nullable=True)
    robust_zscore: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 4), nullable=True)
    attribution_json: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    suggestion_json: Mapped[Any | None] = mapped_column(sa.JSON, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(sa.CHAR(32), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.Enum("open", "acked", "resolved", "ignored", name="alert_status"),
        nullable=False,
        server_default=sa.text("'open'"),
    )
    acked_by: Mapped[int | None] = mapped_column(_U, nullable=True)
    acked_at: Mapped[dt.datetime | None] = mapped_column(_DT3, nullable=True)


class SqlAudit(Base):
    __tablename__ = "sql_audit"
    __table_args__ = {**_TABLE_KW, "comment": "SQL 安全审计"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(sa.CHAR(32), nullable=False)
    user_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    biz_line_id: Mapped[int | None] = mapped_column(_U, nullable=True)
    sql_fingerprint: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    sql_text: Mapped[str] = mapped_column(mysql.MEDIUMTEXT, nullable=False)
    rewritten_sql: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT, nullable=True)
    dialect: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default=sa.text("'mysql'")
    )
    decision: Mapped[str] = mapped_column(
        sa.Enum("allow", "rewrite", "deny", "retry", name="audit_decision"), nullable=False
    )
    deny_reason: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    guard_stage: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    scope_injected: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("0")
    )
    scope_hash: Mapped[str | None] = mapped_column(sa.CHAR(32), nullable=True)
    rows_returned: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    exec_ms: Mapped[int | None] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)


class Outbox(Base):
    """★ P1-6：埋点/成本写入的 **outbox 表**（与业务数据同事务落库）。

    v1 用内存 ``asyncio.Queue`` 做「统一投递」——进程崩溃即丢账，且超时/取消路径更难保证。
    v2 改为：``enqueue()`` 写本表（同步、同事务）→ 投递协程异步消费 → 成功 ``mark_done``，
    失败重试 N 次后落 DLQ 文件并计数 ``telemetry_dropped_total``。

    本表 **不在 §5.2 的 21 张业务表内**，属于 v2 修复 P1-6 所需的**基础设施表**（新增迁移 0002）。
    """

    __tablename__ = "outbox"
    __table_args__ = {**_TABLE_KW, "comment": "统一投递 outbox（P1-6：禁止内存队列）"}

    id: Mapped[int] = mapped_column(_U, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, comment="llm/tool/span/run_doc/es_event"
    )
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Enum("pending", "processing", "done", "failed", name="outbox_status"),
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_DT3, nullable=False, server_default=_NOW3)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3
    )
