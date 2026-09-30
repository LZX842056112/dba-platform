"""worker 任务：日报（可观测性 / 技能 / 记忆 / 异常 快照）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§1.6.6（日报）。

产出：把当日快照写成 JSON，落到对象存储 ``reports/daily/{date}.json``。
★ 诚实降级：对象存储不可用时**不静默假装成功**——返回 ``persisted=False`` 并把摘要写日志，
  由调用方/监控决定是否重试（绝不生成一个「看起来存在」的假对象 Key）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from typing import Any

from ._common import naive_now, parse_date

__all__ = ["run_daily_report"]

logger = logging.getLogger("dba.worker.jobs.report")

_REPORT_BUCKET = "reports"


async def run_daily_report(container: Any, *, stat_date: str | None = None) -> dict[str, Any]:
    """生成并落盘「某日」快照。缺省为昨日（T+1）。"""
    service = container.get("observability_service")
    if service is None:
        logger.warning("日报跳过：observability_service 未装配")
        return {"ok": False, "reason": "observability_not_assembled"}

    target = parse_date(stat_date) or (naive_now().date() - timedelta(days=1))
    since = datetime.combine(target, time.min)
    until = since + timedelta(days=1)

    overview: dict[str, Any] = await service.overview(since=since, until=until)
    skill: Any = await service.skill_metrics(target, None)
    memory: Any = await service.memory_metrics(target, None)
    anomalies: list[dict[str, Any]] = await service.anomalies()
    silent_runs = int(
        (overview.get("kpi_cards", {}).get("silent_failures", {}) or {}).get("value", 0)
    )

    report: dict[str, Any] = {
        "stat_date": target.isoformat(),
        "generated_at": naive_now().isoformat(),
        "overview": overview,
        "skills": {
            "total": int(getattr(skill, "total", 0) or 0),
            "used": int(getattr(skill, "used", 0) or 0),
            "reuse_rate": float(getattr(skill, "reuse_rate", 0.0) or 0.0),
            "dead_count": int(getattr(skill, "dead_count", 0) or 0),
        },
        "memory": {
            "hit_rate": float(getattr(memory, "hit_rate", 0.0) or 0.0),
            "lookups": int(getattr(memory, "lookups", 0) or 0),
            "hits": int(getattr(memory, "hits", 0) or 0),
        },
        "open_anomalies": len(anomalies),
        "silent_failures": silent_runs,
    }

    object_key = f"daily/{target.isoformat()}.json"
    persisted = False
    store = container.get("object_store")
    if store is not None:
        try:
            body = json.dumps(report, ensure_ascii=False, default=str).encode("utf-8")
            await store.put(_REPORT_BUCKET, object_key, body, "application/json")
            persisted = True
        except Exception as exc:  # noqa: BLE001 - 落盘失败不抛（由监控感知 persisted=False）
            logger.warning("日报落盘失败：%s", exc)
    else:
        logger.warning("日报未落盘：对象存储不可用（persisted=False）")

    logger.info(
        "日报完成 stat_date=%s anomalies=%d silent=%d persisted=%s",
        target,
        len(anomalies),
        silent_runs,
        persisted,
    )
    return {
        "ok": True,
        "stat_date": target.isoformat(),
        "report": report,
        "object_bucket": _REPORT_BUCKET,
        "object_key": object_key,
        "persisted": persisted,
    }
