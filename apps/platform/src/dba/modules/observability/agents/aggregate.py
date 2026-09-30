"""模块 03 · 四角色 Agent 之二：聚合（aggregate）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§3.19。

职责：把采集到的 Run 明细聚合为 ``metric_daily``（rollup）。真正的聚合算法在
``rollup.RollupService``（纯逻辑、可单测），本 Agent 只是把它接到流水线上，
并负责从 payload 解析 ``stat_date`` 与业务线。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from dba_runtime import AgentOutput, RunContext, traced

from ..rollup import RollupService

__all__ = ["AggregateAgent"]

logger = logging.getLogger("dba.modules.observability.aggregate")


class AggregateAgent:
    """聚合 Agent：rollup → ``metric_daily``（幂等全量重算，P2-9）。"""

    name = "aggregate"

    def __init__(self, rollup: RollupService) -> None:
        self._rollup = rollup

    @traced("step.obs.aggregate", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        stat_date = _stat_date_of(payload)
        biz_line_id = payload.get("biz_line_id", ctx.biz_line_id)
        report = await self._rollup.run(stat_date, biz_line_id=biz_line_id)
        return AgentOutput(
            data={
                "rollup": {
                    "stat_date": report.stat_date.isoformat(),
                    "rows": report.rows,
                    "run_count": report.run_count,
                    "self_run_count": report.self_run_count,
                    "rollup_run_at": report.rollup_run_at.isoformat(),
                }
            },
            confidence=1.0,
            meta={"agent": self.name},
        )


def _stat_date_of(payload: dict[str, Any]) -> date:
    """从 payload 解析统计日（默认「昨天」——聚合昨天已完结的数据）。"""
    value = payload.get("stat_date")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            logger.warning("stat_date 解析失败，回退为昨天：%s", value)
    return datetime.now(UTC).date() - timedelta(days=1)
