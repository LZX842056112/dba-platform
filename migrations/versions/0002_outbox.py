"""outbox 投递表（★ P1-6：禁止内存队列）。

Revision ID: 0002_outbox
Revises: 0001_initial_schema
Create Date: 2026-09-29

为什么必须有这张表（照抄 v1 会怎样错）
------------------------------------
v1 用进程内 ``asyncio.Queue`` 做「统一投递」：埋点在 ``finally`` 里入队，后台协程消费。
问题有三：
  1) 进程崩溃 / 被 kill -9，队列里未投递的埋点与成本**永久丢失**（账不平）；
  2) 多 worker 部署时队列是**进程私有的**，无法共享、无法回放；
  3) 无法对「投递失败」做持久化重试与 DLQ 对账。

v2 改为 **transactional outbox**：埋点与业务数据**同事务**写入本表 →
投递协程 ``claim()``（``FOR UPDATE SKIP LOCKED``）异步消费 → 成功 ``mark_done``，
失败累加 ``attempts`` 并在超阈值后置 ``failed``（落 DLQ 文件 + 计数
``telemetry_dropped_total``）。因此 kill -9 后未投递记录仍在表中，重启即可补投。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002_outbox"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

_TK: dict[str, str] = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_U = mysql.BIGINT(unsigned=True)
_DT3 = mysql.DATETIME(fsp=3)
_NOW3 = sa.text("CURRENT_TIMESTAMP(3)")


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", _U, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False, comment="llm/tool/span/run_doc/es_event"),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "done", "failed", name="outbox_status"),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", _DT3, nullable=False, server_default=_NOW3),
        sa.Column("updated_at", _DT3, nullable=False, server_default=_NOW3, server_onupdate=_NOW3),
        sa.Index("idx_outbox_status_id", "status", "id"),
        **_TK,
        comment="统一投递 outbox（P1-6：禁止内存队列）",
    )


def downgrade() -> None:
    op.drop_table("outbox")
