"""worker 任务：冷归档（超期 ``run`` 明细 → 对象存储）。

对齐《设计方案 v2》§6.1.4 与《实现要点清单》§1.6.6。

为什么需要
----------
``run`` 是分区表但**只增不减**：热表无限增长会拖慢 ``/obs/runs`` 与 rollup 扫描。归档把
超过保留期的 Run 导出为 JSONL 落对象存储（廉价冷存储），再（可选）从热表删除。

★ 安全默认：``dry_run=True``——默认**只导出、不删除**。删除是破坏性动作，必须由运维显式
  以 ``dry_run=False`` 触发，且导出成功后才删（导出失败即中止，绝不留「删了没备份」的洞）。
★ 诚实说明：归档等价于「热表瘦身」，被归档的 Run 将不再出现在 ``/obs/runs``；若需长期可查，
  应配套冷查询通道（本批次未实现，见报告「遗留事项」）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ._common import naive_now

__all__ = ["run_archive"]

logger = logging.getLogger("dba.worker.jobs.archive")

_ARCHIVE_BUCKET = "archive"
#: 默认保留 90 天
DEFAULT_RETENTION_DAYS = 90


def _json_default(value: Any) -> str:
    """JSON 序列化兜底：``datetime``/``date`` 转 ISO 字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def run_archive(
    container: Any,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch: int = 2000,
    dry_run: bool = True,
) -> dict[str, Any]:
    """归档一批超期 ``run``。

    :param retention_days: 保留天数（早于该窗口的 Run 才归档）。
    :param batch: 单次最多归档行数（避免长事务）。
    :param dry_run: ``True`` = 只导出不删除（默认）；``False`` = 导出成功后删除。
    """
    repos = container.get("repos")
    mysql = container.get("mysql")
    if repos is None or mysql is None:
        logger.warning("归档跳过：MySQL 未装配")
        return {"ok": False, "reason": "storage_unavailable"}

    from dba.storage.mysql import models as m  # noqa: PLC0415 - 惰性导入

    now = naive_now()
    cutoff = now - timedelta(days=retention_days)
    engine = mysql.rw_engine

    async with engine.connect() as conn:
        result = await conn.execute(
            select(m.Run.__table__)
            .where(m.Run.started_at < cutoff)
            .order_by(m.Run.started_at.asc())
            .limit(batch)
        )
        rows = [dict(row._mapping) for row in result.fetchall()]

    if not rows:
        logger.info("归档无待处理数据 cutoff=%s", cutoff.isoformat())
        return {
            "ok": True,
            "archived": 0,
            "deleted": 0,
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
            "object_key": None,
        }

    stamp = f"{rows[0]['started_at']:%Y%m%d}-{rows[-1]['started_at']:%Y%m%d}"
    object_key = f"run/{stamp}.jsonl"
    body = "\n".join(json.dumps(r, ensure_ascii=False, default=_json_default) for r in rows)

    store = container.get("object_store")
    if store is None:
        # ★ 无对象存储 → 绝不删除（宁可留热表，也不能「删了没备份」）
        logger.warning("归档中止：对象存储不可用（未删除任何行）")
        return {
            "ok": False,
            "reason": "object_store_unavailable",
            "candidates": len(rows),
            "dry_run": dry_run,
        }

    try:
        await store.put(_ARCHIVE_BUCKET, object_key, body.encode("utf-8"), "application/x-ndjson")
    except Exception as exc:  # noqa: BLE001 - 导出失败即中止（不进入删除分支）
        logger.warning("归档导出失败（未删除任何行）：%s", exc)
        return {"ok": False, "reason": "export_failed", "error": str(exc), "candidates": len(rows)}

    deleted = 0
    if not dry_run:
        keys = [(str(r["trace_id"]), r["started_at"]) for r in rows]
        try:
            async with engine.begin() as conn:
                for trace_id, started_at in keys:
                    await conn.execute(
                        delete(m.Run).where(
                            m.Run.trace_id == trace_id, m.Run.started_at == started_at
                        )
                    )
                    deleted += 1
        except Exception as exc:  # noqa: BLE001 - 删除中途失败：已删的留在冷存（不丢数据）
            logger.warning("归档删除中断 deleted=%d：%s", deleted, exc)

    logger.info(
        "归档完成 rows=%d deleted=%d dry_run=%s object=%s",
        len(rows),
        deleted,
        dry_run,
        object_key,
    )
    return {
        "ok": True,
        "archived": len(rows),
        "deleted": deleted,
        "dry_run": dry_run,
        "cutoff": cutoff.isoformat(),
        "object_bucket": _ARCHIVE_BUCKET,
        "object_key": object_key,
    }
