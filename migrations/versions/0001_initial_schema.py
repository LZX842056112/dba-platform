"""初始schema：v2 §5.2 的 21 张业务表逐字落地。

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-29

★ 表数与分组（§5.2）：
  §5.2.1 组织与权限 5：biz_line / auth_user / auth_role / auth_user_role / row_scope_rule
  §5.2.2 语义层     3：sem_metric / sem_field_mapping / sem_dict_entry
  §5.2.3 Run 埋点   5：app_agent / run / llm_call / tool_call / price_book
  §5.2.4 预算       3：budget / budget_usage + ★v2 新增 budget_reservation
  §5.2.5 技能聚合   5：skill_registry / skill_usage / metric_daily / alert_event / sql_audit
  合计 **21** 张。（《实现要点清单》§1 写的「20 张」为 v1 口径，v2 补预算预留表后为 21。）

★ 关键设计（照抄 v1 会怎样错）：
  * run / llm_call / tool_call **只留 trace_id 软关联、不建外键**（§5.1）——高写入路径建外键
    会拖慢吞吐并妨碍分区裁剪；
  * run / metric_daily 用 RANGE 分区，分区列必须是唯一键的一部分，否则 MySQL 拒绝建表；
  * hard_limit_pct 默认 **90**（P0-5，v1 默认 100 导致 HARD 档永不命中）；
  * llm_call.prompt_tokens 是**总输入**，计费时用 ``prompt_tokens - cached_tokens``（P0-3）；
  * usage_source 区分 measured / self_reported，self_reported **不计入预算**（P1-5）；
  * metric_daily.biz_line_id 哨兵为 **0**（P1-1，v1 用 -1 与 Milvus 分区键不一致）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

_TK: dict[str, str] = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_U = mysql.BIGINT(unsigned=True)
_DT3 = mysql.DATETIME(fsp=3)
_NOW3 = sa.text("CURRENT_TIMESTAMP(3)")


def upgrade() -> None:
    # ── §5.2.1 组织与权限 ─────────────────────────────────────────────
    op.create_table(
        "biz_line",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("parent_id", _U, sa.ForeignKey("biz_line.id"), nullable=True),
        sa.Column("manager_user_id", _U, nullable=True),
        sa.Column("status", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("deleted_at", _DT3, nullable=True),
        sa.UniqueConstraint("code", name="uq_biz_line_code"),
        **_TK,
        comment="业务线",
    )

    op.create_table(
        "auth_user",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("email", sa.String(128), nullable=True),
        sa.Column("biz_line_id", _U, sa.ForeignKey("biz_line.id"), nullable=True),
        sa.Column("status", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("deleted_at", _DT3, nullable=True),
        sa.UniqueConstraint("username", name="uq_auth_user_username"),
        **_TK,
        comment="用户",
    )

    op.create_table(
        "auth_role",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.UniqueConstraint("code", name="uq_auth_role_code"),
        **_TK,
        comment="角色",
    )

    op.create_table(
        "auth_user_role",
        sa.Column("user_id", _U, sa.ForeignKey("auth_user.id"), primary_key=True),
        sa.Column("role_id", _U, sa.ForeignKey("auth_role.id"), primary_key=True),
        **_TK,
        comment="用户-角色关联",
    )

    op.create_table(
        "row_scope_rule",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("role_id", _U, sa.ForeignKey("auth_role.id"), nullable=False),
        sa.Column("physical_table", sa.String(128), nullable=False),
        sa.Column("scope_column", sa.String(128), nullable=False),
        sa.Column(
            "operator",
            sa.Enum("IN", "=", "NOT IN", "LIKE", "BETWEEN", name="rsr_operator"),
            nullable=False,
            server_default=sa.text("'IN'"),
        ),
        sa.Column(
            "value_type",
            sa.Enum("STATIC", "CTX_VAR", name="rsr_value_type"),
            nullable=False,
            server_default=sa.text("'STATIC'"),
        ),
        sa.Column("value_json", sa.JSON, nullable=True),
        sa.Column("ctx_var", sa.String(128), nullable=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("100")),
        sa.Column("enabled", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("updated_at", _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3),
        sa.Index("idx_rsr_role", "role_id", "enabled"),
        **_TK,
        comment="行级权限规则（下沉到 SQL 生成阶段的硬约束）",
    )

    # ── §5.2.2 语义层 ────────────────────────────────────────────────
    op.create_table(
        "sem_metric",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("metric_code", sa.String(128), nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("biz_line_id", _U, sa.ForeignKey("biz_line.id"), nullable=True),
        sa.Column("caliber_desc", sa.Text, nullable=False),
        sa.Column("sql_expr", sa.Text, nullable=False),
        sa.Column("unit", sa.String(32), nullable=False, server_default=sa.text("'CNY'")),
        sa.Column("include_tax", sa.SmallInteger, nullable=True),
        sa.Column("region_scope", sa.JSON, nullable=True),
        sa.Column("default_dims", sa.JSON, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", _U, nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("updated_at", _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3),
        sa.UniqueConstraint("metric_code", "biz_line_id", "version", name="uq_sem_metric"),
        sa.Index("idx_sem_metric_name", "metric_name"),
        **_TK,
        comment="指标口径定义（权威源）",
    )

    op.create_table(
        "sem_field_mapping",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("metric_id", _U, sa.ForeignKey("sem_metric.id"), nullable=True),
        sa.Column("logical_field", sa.String(128), nullable=False),
        sa.Column("physical_table", sa.String(128), nullable=False),
        sa.Column("physical_column", sa.String(128), nullable=False),
        sa.Column("join_path", sa.JSON, nullable=True),
        sa.Column("is_dimension", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("sample_values", sa.JSON, nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Index("idx_sfm_logical", "logical_field"),
        **_TK,
        comment="逻辑字段 → 物理表列映射",
    )

    op.create_table(
        "sem_dict_entry",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("term", sa.String(128), nullable=False),
        sa.Column("synonyms", sa.JSON, nullable=True),
        sa.Column("metric_id", _U, sa.ForeignKey("sem_metric.id"), nullable=True),
        sa.Column("biz_line_id", _U, sa.ForeignKey("biz_line.id"), nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("updated_at", _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3),
        sa.Index("idx_sde_term", "term"),
        **_TK,
        comment="口径词典（结构化部分）",
    )

    # ── §5.2.3 Run 埋点与成本明细 ────────────────────────────────────
    op.create_table(
        "app_agent",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("agent_uid", sa.CHAR(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("biz_line_id", _U, sa.ForeignKey("biz_line.id"), nullable=True),
        sa.Column("owner_user_id", _U, nullable=True),
        sa.Column(
            "runtime_type",
            sa.Enum("internal_pipeline", "external_sdk", "otel_agent", name="agent_runtime_type"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=True),
        sa.Column("entrypoint", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "idle", "retired", name="agent_status"),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("tags", sa.JSON, nullable=True),
        sa.Column("registered_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("last_heartbeat_at", _DT3, nullable=True),
        sa.UniqueConstraint("agent_uid", name="uq_app_agent_uid"),
        **_TK,
        comment="Agent 注册中心",
    )

    op.create_table(
        "run",
        sa.Column("trace_id", sa.CHAR(32), primary_key=True),
        sa.Column("started_at", _DT3, primary_key=True),
        sa.Column(
            "module",
            sa.Enum("chatbi", "observability", "finops", "system", name="run_module"),
            nullable=False,
        ),
        sa.Column("user_id", _U, nullable=True),
        sa.Column("biz_line_id", _U, nullable=True),
        sa.Column("agent_uid", sa.CHAR(36), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("parent_trace", sa.CHAR(32), nullable=True),
        sa.Column("scope_hash", sa.CHAR(32), nullable=True),
        sa.Column(
            "status",
            sa.Enum("running", "success", "failed", "timeout", "aborted", name="run_status"),
            nullable=False,
        ),
        sa.Column("ended_at", _DT3, nullable=True),
        sa.Column("latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("span_count", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_calls", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_calls", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_in", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("cost_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Index("idx_run_biz_started", "biz_line_id", "started_at"),
        sa.Index("idx_run_module_status", "module", "status", "started_at"),
        **_TK,
        comment="Run 元数据（按月分区）",
        # ★ RANGE 分区：分区列 started_at 必须是主键的一部分（已满足）
        mysql_partition_by=(
            "RANGE COLUMNS(started_at) ("
            "PARTITION p202609 VALUES LESS THAN ('2026-10-01'),"
            "PARTITION p202610 VALUES LESS THAN ('2026-11-01'),"
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        ),
    )

    op.create_table(
        "llm_call",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.CHAR(32), nullable=False),
        sa.Column("span_id", sa.CHAR(16), nullable=False),
        sa.Column("biz_line_id", _U, nullable=True),
        sa.Column("agent_uid", sa.CHAR(36), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("route_policy", sa.String(64), nullable=True),
        sa.Column("downgrade_from", sa.String(64), nullable=True),
        # ★ prompt_tokens 是**总输入**；计费输入 = prompt_tokens - cached_tokens（P0-3）
        sa.Column("prompt_tokens", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("completion_tokens", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("price_book_id", _U, nullable=True),
        sa.Column("cost_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("ttft_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("cache_hit", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Enum("ok", "error", "timeout", name="llm_status"), nullable=False),
        sa.Column(
            "usage_source",
            sa.Enum("measured", "self_reported", name="usage_source"),
            nullable=False,
            server_default=sa.text("'measured'"),
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Index("idx_llm_trace", "trace_id"),
        sa.Index("idx_llm_model_created", "model", "created_at"),
        **_TK,
        comment="LLM 调用明细",
    )

    op.create_table(
        "tool_call",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.CHAR(32), nullable=False),
        sa.Column("span_id", sa.CHAR(16), nullable=False),
        sa.Column("biz_line_id", _U, nullable=True),
        sa.Column("agent_uid", sa.CHAR(36), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("args_hash", sa.CHAR(64), nullable=True),
        sa.Column("status", sa.Enum("ok", "error", name="tool_status"), nullable=False),
        sa.Column("latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("cost_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Index("idx_tool_trace", "trace_id"),
        **_TK,
        comment="工具调用明细",
    )

    op.create_table(
        "price_book",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column(
            "billing_unit",
            sa.Enum("PER_1K_TOKEN", "PER_CALL", "PER_SECOND", name="billing_unit"),
            nullable=False,
        ),
        sa.Column("input_price_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("output_price_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("cache_read_price_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("cache_write_price_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default=sa.text("'USD'")),
        sa.Column("fx_rate_to_usd", sa.Numeric(18, 8), nullable=False, server_default=sa.text("1.0")),
        sa.Column("tier_json", sa.JSON, nullable=True),
        sa.Column("effective_from", _DT3, nullable=False),
        sa.Column("effective_to", _DT3, nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Index("idx_price_lookup", "provider", "model", "effective_from"),
        **_TK,
        comment="归一化价格表",
    )

    # ── §5.2.4 预算 ──────────────────────────────────────────────────
    op.create_table(
        "budget",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column(
            "scope_type",
            sa.Enum("GLOBAL", "BIZ_LINE", "AGENT", "TEAM", name="budget_scope_type"),
            nullable=False,
        ),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("period", sa.Enum("DAY", "WEEK", "MONTH", name="budget_period"), nullable=False),
        sa.Column("amount_micro_usd", _U, nullable=False),
        sa.Column("soft_limit_pct", sa.SmallInteger, nullable=False, server_default=sa.text("80")),
        # ★ P0-5：默认 90（v1 默认 100 会让 HARD 档永不命中）
        sa.Column("hard_limit_pct", sa.SmallInteger, nullable=False, server_default=sa.text("90")),
        sa.Column(
            "soft_action",
            sa.Enum("ALERT", "DOWNGRADE_MODEL", "COMPRESS_CONTEXT", "RATE_LIMIT", name="budget_soft_action"),
            nullable=False,
            server_default=sa.text("'ALERT'"),
        ),
        sa.Column(
            "hard_action",
            sa.Enum("BLOCK", "CIRCUIT_BREAK", name="budget_hard_action"),
            nullable=False,
            server_default=sa.text("'BLOCK'"),
        ),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("100")),
        sa.Column("timezone", sa.String(64), nullable=False, server_default=sa.text("'Asia/Shanghai'")),
        sa.Column("enabled", sa.SmallInteger, nullable=False, server_default=sa.text("1")),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("updated_at", _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3),
        sa.UniqueConstraint("scope_type", "scope_id", "period", "version", name="uq_budget"),
        **_TK,
        comment="分级预算",
    )

    op.create_table(
        "budget_usage",
        sa.Column("budget_id", _U, sa.ForeignKey("budget.id"), primary_key=True),
        sa.Column("period_start", sa.Date, primary_key=True),
        sa.Column("consumed_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("call_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "breaker_state",
            sa.Enum("CLOSED", "HALF_OPEN", "OPEN", name="breaker_state"),
            nullable=False,
            server_default=sa.text("'CLOSED'"),
        ),
        sa.Column("breaker_opened_at", _DT3, nullable=True),
        sa.Column("updated_at", _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3),
        **_TK,
        comment="预算用量（权威账本；快路径在 Redis）",
    )

    # ★ v2 新增：预算预留明细（P0-4 幂等结算的权威落点）
    op.create_table(
        "budget_reservation",
        sa.Column("reservation_id", sa.CHAR(26), primary_key=True),
        sa.Column("budget_id", _U, sa.ForeignKey("budget.id"), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("trace_id", sa.CHAR(32), nullable=True),
        sa.Column("estimated_micro_usd", _U, nullable=False),
        sa.Column("actual_micro_usd", _U, nullable=True),
        sa.Column(
            "state",
            sa.Enum("RESERVED", "SETTLED", "RELEASED", "EXPIRED", name="rsv_state"),
            nullable=False,
            server_default=sa.text("'RESERVED'"),
        ),
        sa.Column("decision", sa.SmallInteger, nullable=False),
        sa.Column("expires_at", _DT3, nullable=False),
        sa.Column("settled_at", _DT3, nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Index("idx_rsv_budget_period_state", "budget_id", "period_start", "state"),
        sa.Index("idx_rsv_trace", "trace_id"),
        **_TK,
        comment="预算预留明细（幂等结算的依据）",
    )

    # ── §5.2.5 技能、聚合与审计 ──────────────────────────────────────
    op.create_table(
        "skill_registry",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("skill_key", sa.String(128), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("biz_line_id", _U, sa.ForeignKey("biz_line.id"), nullable=True),
        sa.Column("meta_json", sa.JSON, nullable=False),
        sa.Column("steps_json", sa.JSON, nullable=False),
        sa.Column("params_schema", sa.JSON, nullable=False),
        sa.Column("parent_skill", sa.String(128), nullable=True),
        sa.Column("owner_user_id", _U, nullable=True),
        sa.Column("usage_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("reuse_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("success_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("fail_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("last_used_at", _DT3, nullable=True),
        sa.Column("is_dead", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "archived", name="skill_status"),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.UniqueConstraint("skill_key", "version", name="uq_skill_key_version"),
        **_TK,
        comment="技能注册表（元数据 + 步骤 + 参数三层）",
    )

    op.create_table(
        "skill_usage",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("skill_id", _U, sa.ForeignKey("skill_registry.id"), nullable=False),
        sa.Column("trace_id", sa.CHAR(32), nullable=False),
        sa.Column("biz_line_id", _U, nullable=True),
        sa.Column("is_reuse", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_saved_est", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("baseline_cost_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        # ★ P1-2：自愈重试会重复经过同一技能 → 用唯一键 + upsert 防灌水
        sa.UniqueConstraint("skill_id", "trace_id", name="uq_skill_usage_trace"),
        **_TK,
        comment="技能使用记录",
    )

    op.create_table(
        "metric_daily",
        sa.Column("stat_date", sa.Date, primary_key=True),
        # ★ P1-1：哨兵 0（v1 用 -1，与 Milvus 分区键哨兵 0 不一致）
        sa.Column("biz_line_id", _U, primary_key=True, server_default=sa.text("0")),
        sa.Column("agent_uid", sa.CHAR(36), primary_key=True, server_default=sa.text("''")),
        sa.Column("model", sa.String(64), primary_key=True, server_default=sa.text("''")),
        sa.Column("run_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("success_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("fail_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("timeout_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("silent_fail_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_in", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("cost_micro_usd", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("avg_latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("p50_latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("p95_latency_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("cache_hit_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("downgrade_count", _U, nullable=False, server_default=sa.text("0")),
        sa.Column("skill_total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("skill_used", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("skill_reuse_rate", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("dead_skill_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("mem_lookup", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("mem_hit", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("mem_hit_rate", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_saved_est", _U, nullable=False, server_default=sa.text("0")),
        **_TK,
        comment="每日聚合指标（按月分区）",
        mysql_partition_by=(
            "RANGE COLUMNS(stat_date) ("
            "PARTITION p202609 VALUES LESS THAN ('2026-10-01'),"
            "PARTITION p202610 VALUES LESS THAN ('2026-11-01'),"
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        ),
    )

    op.create_table(
        "alert_event",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("detected_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column(
            "severity",
            sa.Enum("info", "warn", "critical", name="alert_severity"),
            nullable=False,
            server_default=sa.text("'warn'"),
        ),
        sa.Column(
            "category",
            sa.Enum(
                "cost_spike", "fail_rate_up", "skill_rot", "silent_failure", "loop_suspect", name="alert_category"
            ),
            nullable=False,
        ),
        sa.Column("scope_type", sa.Enum("BIZ_LINE", "AGENT", "GLOBAL", name="alert_scope_type"), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=True),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("observed_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("baseline_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("robust_zscore", sa.Numeric(10, 4), nullable=True),
        sa.Column("attribution_json", sa.JSON, nullable=True),
        sa.Column("suggestion_json", sa.JSON, nullable=True),
        sa.Column("trace_id", sa.CHAR(32), nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "acked", "resolved", "ignored", name="alert_status"),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("acked_by", _U, nullable=True),
        sa.Column("acked_at", _DT3, nullable=True),
        sa.Index("idx_alert_status_detected", "status", "detected_at"),
        **_TK,
        comment="异常事件",
    )

    op.create_table(
        "sql_audit",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.CHAR(32), nullable=False),
        sa.Column("user_id", _U, nullable=True),
        sa.Column("biz_line_id", _U, nullable=True),
        sa.Column("sql_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("sql_text", mysql.MEDIUMTEXT, nullable=False),
        sa.Column("rewritten_sql", mysql.MEDIUMTEXT, nullable=True),
        sa.Column("dialect", sa.String(16), nullable=False, server_default=sa.text("'mysql'")),
        sa.Column("decision", sa.Enum("allow", "rewrite", "deny", "retry", name="audit_decision"), nullable=False),
        sa.Column("deny_reason", sa.String(255), nullable=True),
        sa.Column("guard_stage", sa.String(32), nullable=True),
        sa.Column("scope_injected", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("scope_hash", sa.CHAR(32), nullable=True),
        sa.Column("rows_returned", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("exec_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Index("idx_audit_trace", "trace_id"),
        sa.Index("idx_audit_decision_created", "decision", "created_at"),
        **_TK,
        comment="SQL 安全审计",
    )


def downgrade() -> None:
    for name in (
        "sql_audit",
        "alert_event",
        "metric_daily",
        "skill_usage",
        "skill_registry",
        "budget_reservation",
        "budget_usage",
        "budget",
        "price_book",
        "tool_call",
        "llm_call",
        "run",
        "app_agent",
        "sem_dict_entry",
        "sem_field_mapping",
        "sem_metric",
        "row_scope_rule",
        "auth_user_role",
        "auth_role",
        "auth_user",
        "biz_line",
    ):
        op.drop_table(name)
