"""worker 任务：滞留 Run 收口（stale run）。

对齐《实现要点清单》§1.6.6 与 DoD#5（滞留 Run 15 分钟收口）。

为什么需要（照抄 v1 会怎样错）
------------------------------
进程被 kill / 机器重启时，``run`` 里会留下永远 ``status='running'`` 的行。v1 没有收口任务：
* ``/obs/overview`` 的「当前运行中」永远虚高；
* rollup 把这些僵尸 Run 计入 ``run_count``，``success_rate`` 被系统性拉低；
* 且因 ``run`` 按月分区，僵尸行会随分区一起长期留存。

口径：把 ``started_at < now - threshold`` 且仍 ``running`` 的 Run 收口为 ``timeout``，
``error_code='STALE_RUN_TIMEOUT'``，``ended_at=now``。**只改状态、不动成本**（成本应由明细累加，
不得由收口任务臆造）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ._common import naive_now

__all__ = ["close_stale_runs"]

logger = logging.getLogger("dba.worker.jobs.stale_run")

#: 超过该时长仍 running 视为滞留（DoD#5：15 分钟）
STALE_THRESHOLD_MINUTES = 15
#: 扫描回溯上限（避免历史全表扫描；run 表按月分区）
_SCAN_MAX_AGE_DAYS = 7


async def close_stale_runs(
    container: Any,
    *,
    threshold_minutes: int = STALE_THRESHOLD_MINUTES,
    max_age_days: int = _SCAN_MAX_AGE_DAYS,
    limit: int = 5000,
) -> dict[str, Any]:
    """把滞留的 ``running`` Run 收口为 ``timeout``。"""
    repos = container.get("repos")
    if repos is None:
        logger.warning("滞留收口跳过：repos 未装配")
        return {"ok": False, "reason": "storage_unavailable"}

    now = naive_now()
    cutoff = now - timedelta(minutes=threshold_minutes)
    oldest = now - timedelta(days=max_age_days)

    runs: list[dict[str, Any]] = await repos.run.list_runs(
        {"status": "running", "since": oldest, "until": cutoff},
        limit=limit,
    )

    closed = 0
    failed = 0
    for run in runs:
        trace_id = str(run.get("trace_id") or "")
        started_at = run.get("started_at")
        if not trace_id or started_at is None:
            continue
        latency_ms = max(0, int((now - started_at).total_seconds() * 1000))
        row: dict[str, Any] = {
            "status": "timeout",
            "ended_at": now,
            "latency_ms": latency_ms,
            "error_code": "STALE_RUN_TIMEOUT",
        }
        try:
            await repos.run.finish(trace_id, started_at=started_at, row=row)
            closed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余
            logger.warning("收口 Run 失败 trace_id=%s：%s", trace_id, exc)
            failed += 1

    logger.info(
        "滞留 Run 收口完成 scanned=%d closed=%d failed=%d cutoff=%s",
        len(runs),
        closed,
        failed,
        cutoff.isoformat(),
    )
    return {
        "ok": True,
        "scanned": len(runs),
        "closed": closed,
        "failed": failed,
        "cutoff": cutoff.isoformat(),
        "threshold_minutes": threshold_minutes,
    }
