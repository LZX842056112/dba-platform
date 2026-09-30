"""worker 任务：指标 rollup（``run`` 明细 → ``metric_daily``）。

对齐《设计方案 v2》§6.4、§5.2.5 与《实现要点清单》§6.7、P2-9。
真实计算委托给模块 03 的 ``RollupService``（owner=observability），worker 只负责调度与
时间窗解析——**不重复实现聚合逻辑**（否则两处口径必然漂移）。

★ P2-9 幂等：``RollupService.run`` 是「全量重算 + 主键 upsert」，重跑不翻倍；
  故本任务**可安全重试**，无需外部水位表（跨进程水位见报告「遗留问题」）。
★ P1-4：``RollupService`` 内部已 ``exclude_modules=("observability","finops")``，
  平台自身 Run 不进业务指标。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ._common import naive_now, parse_date

__all__ = ["run_rollup"]

logger = logging.getLogger("dba.worker.jobs.rollup")


async def run_rollup(
    container: Any,
    *,
    stat_date: str | None = None,
    biz_line_id: int | None = None,
    days_back: int = 1,
) -> dict[str, Any]:
    """执行一次 rollup。

    :param stat_date: 目标统计日（ISO 字符串）；缺省为 ``today - days_back``。
    :param biz_line_id: 仅重算某业务线；缺省 ``None`` = 一次算出所有业务线 + 哨兵 0。
    :param days_back: 缺省回溯天数（T+1 跑昨日）。
    """
    bundle = container.get("observability")
    rollup = getattr(bundle, "rollup", None) if bundle is not None else None
    if rollup is None:
        logger.warning("rollup 任务跳过：observability 模块未装配")
        return {"ok": False, "reason": "observability_not_assembled"}

    target = parse_date(stat_date) or (naive_now().date() - timedelta(days=days_back))

    # 缺省单次调用（biz_line_id=None）即产出全部业务线 + 哨兵 0 的行；
    # 逐业务线分别调用会与全局行重复计入，故缺省不做循环。
    lines: list[int | None] = [biz_line_id]
    reports: list[dict[str, Any]] = []
    for line in lines:
        report = await rollup.run(target, biz_line_id=line)
        reports.append(
            {
                "stat_date": report.stat_date.isoformat(),
                "biz_line_id": line,
                "rows": int(report.rows),
                "run_count": int(report.run_count),
                "self_run_count": int(report.self_run_count),
                "rollup_run_at": report.rollup_run_at.isoformat(),
            }
        )

    total_rows = sum(int(r["rows"]) for r in reports)
    logger.info("rollup 任务完成 stat_date=%s rows=%d", target, total_rows)
    return {
        "ok": True,
        "stat_date": target.isoformat(),
        "reports": reports,
        "total_rows": total_rows,
    }
