"""性能索引补齐（消除 reconcile/rollup/overview 的全表扫）。

Revision ID: 0003_perf_indexes
Revises: 0002_outbox
Create Date: 2026-10-01

背景
----
* ``reconcile`` 按 ``llm_call.created_at`` / ``tool_call.created_at`` 范围过滤，
  但仅有 ``(model, created_at)`` 复合索引，纯 ``created_at`` 范围无法命中 → 全表扫。
* ``rollup`` / ``overview`` 只按 ``run.started_at`` 时间窗过滤（``biz_line_id=None``），
  现有 ``(biz_line_id, started_at)`` / ``(module, status, started_at)`` 均无法命中 → 分区内全扫。
* ``llm_call`` 的 ``idx_llm_trace`` 已在 0001 DDL 建立，但 ORM 模型未声明（DDL/模型漂移），
  本迁移在代码侧（models.py）补声明，避免 ``alembic autogenerate`` 误报。

本迁移只加索引（非唯一，不触碰分区键约束），幂等可重跑。
"""

from __future__ import annotations

from alembic import op

revision = "0003_perf_indexes"
down_revision = "0002_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # reconcile 明细累加按 created_at 范围过滤（见 worker/jobs/reconcile.py）
    op.create_index("idx_llm_created", "llm_call", ["created_at"])
    op.create_index("idx_tool_created", "tool_call", ["created_at"])
    # rollup / overview 只带 started_at 时间窗（见 modules/observability/rollup.py）
    op.create_index("idx_run_started", "run", ["started_at"])


def downgrade() -> None:
    op.drop_index("idx_run_started", table_name="run")
    op.drop_index("idx_tool_created", table_name="tool_call")
    op.drop_index("idx_llm_created", table_name="llm_call")
