"""worker 任务：分区维护（``metric_daily`` 按月 RANGE 分区）。

对齐《设计方案 v2》§5.2.5（``metric_daily`` 按月分区）与《实现要点清单》§1.6.6。

为什么要定时维护（照抄 v1 会怎样错）
------------------------------------
``metric_daily`` 的 DDL 只预建到「下一个月 + pmax」；进入新月份后，所有新数据都会堆进
``pmax``，**分区裁剪彻底失效**——查询会退化成全表扫描，且 pmax 无限膨胀。v1 假设「建表时
多建几个月就够」，线上跑满两个月即出问题。

做法
----
把 ``pmax`` 用 ``REORGANIZE PARTITION`` 拆出「下个月的分区」，**幂等**：已存在的月份跳过。
仅对**已分区**的表操作；未分区（如测试自建表）直接返回 ``table_not_partitioned``，绝不
悄悄改动表结构。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from ._common import naive_now

__all__ = ["maintain_partitions"]

logger = logging.getLogger("dba.worker.jobs.partition")

#: 仅维护这一张表（其余表按需扩展）
_TABLE = "metric_daily"

_EXISTING_SQL = text(
    "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND PARTITION_NAME IS NOT NULL"
)

_MAXX = "pmax"


def _target_months(months_ahead: int) -> list[tuple[str, str]]:
    """返回待建分区 ``[(分区名, LESS THAN 值), ...]``（从下个月起，共 ``months_ahead`` 个）。

    例：2026-09 → ``[("p202610", "2026-11-01"), ("p202611", "2026-12-01"), ...]``
    即「pYYYYMM 的上界 = 下月 1 日」。
    """
    this_month = naive_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (this_month + timedelta(days=32)).replace(day=1)
    out: list[tuple[str, str]] = []
    for i in range(max(1, months_ahead)):
        month_start = (next_month + timedelta(days=32 * i)).replace(day=1)
        upper = (month_start + timedelta(days=32)).replace(day=1)
        out.append((f"p{month_start:%Y%m}", upper.date().isoformat()))
    return out


async def maintain_partitions(container: Any, *, months_ahead: int = 3) -> dict[str, Any]:
    """确保未来 ``months_ahead`` 个月的 ``metric_daily`` 分区存在（幂等）。"""
    mysql = container.get("mysql")
    if mysql is None:
        logger.warning("分区维护跳过：MySQL 不可用")
        return {"ok": False, "reason": "mysql_unavailable"}
    engine = mysql.rw_engine

    async with engine.connect() as conn:
        result = await conn.execute(_EXISTING_SQL, {"t": _TABLE})
        existing = {str(row[0]) for row in result.fetchall()}

    if _MAXX not in existing:
        # 未分区（或分区结构不含 pmax）→ 本任务不改表结构，交人工确认
        logger.warning("分区维护跳过：%s 未按预期分区（partitions=%s）", _TABLE, sorted(existing))
        return {
            "ok": False,
            "reason": "table_not_partitioned",
            "partitions": sorted(existing),
        }

    created: list[str] = []
    skipped: list[str] = []
    for name, upper in _target_months(months_ahead):
        if name in existing:
            skipped.append(name)
            continue
        ddl = (
            f"ALTER TABLE {_TABLE} REORGANIZE PARTITION {_MAXX} INTO ("
            f"PARTITION {name} VALUES LESS THAN ('{upper}'), "
            f"PARTITION {_MAXX} VALUES LESS THAN (MAXVALUE))"
        )
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(ddl)
            created.append(name)
            logger.info("已创建分区 %s (LESS THAN %s)", name, upper)
        except Exception as exc:  # noqa: BLE001 - 单个分区失败不影响其余
            logger.warning("创建分区 %s 失败：%s", name, exc)

    return {
        "ok": True,
        "table": _TABLE,
        "created": created,
        "skipped": skipped,
        "months_ahead": months_ahead,
    }
