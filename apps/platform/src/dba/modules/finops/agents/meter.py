"""模块 10 · 四角色 Agent 之一：计量（meter）。

对齐《设计方案 v2》§6.5 与《实现要点清单》§3.25。

职责：产出成本概览（总量 / 按模块 / 按业务线 / 按模型）与**计价覆盖率**
（``priced_ratio``——「成本可信」的第一证据，§7.5 ``/finops/cost/coverage``）。

★ 归属铁律（§5.9）：模块 10 **不直连 ``metric_daily``**，成本时序一律经
``ObservabilityService.timeseries``（owner=observability）读取。本 Agent 因此持有
的是 ``observability`` 服务引用，而不是任何 MySQL Repository。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dba_runtime import AgentOutput, RunContext, traced

__all__ = ["CostSummary", "MeterAgent"]

logger = logging.getLogger("dba.modules.finops.meter")


@dataclass(slots=True)
class CostSummary:
    """成本概览（``GET /finops/cost/summary``）。"""

    total_micro_usd: int = 0
    by_biz_line: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)
    by_module: dict[str, int] = field(default_factory=dict)
    runs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total_micro_usd,
            "by_biz_line": self.by_biz_line,
            "by_model": self.by_model,
            "by_module": self.by_module,
            "runs": self.runs,
        }


class MeterAgent:
    """计量 Agent：从 ``metric_daily``（经 observability 服务）汇总成本。"""

    name = "meter"

    def __init__(self, *, observability: Any = None) -> None:
        self._obs = observability

    @traced("step.finops.meter", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        window = _window_of(payload)
        biz_line_id = payload.get("biz_line_id", ctx.biz_line_id)
        summary = await self.summarize(window[0], window[1], biz_line_id)
        return AgentOutput(
            data={"cost_summary": summary.as_dict()},
            confidence=1.0,
            meta={"agent": self.name},
        )

    async def summarize(
        self, since: datetime, until: datetime, biz_line_id: int | None
    ) -> CostSummary:
        """按维度汇总成本（数据源：``ObservabilityService.timeseries``）。"""
        summary = CostSummary()
        if self._obs is None:
            return summary
        try:
            rows = await self._obs.timeseries(
                metric="cost_micro_usd", since=since, until=until, biz_line_id=biz_line_id
            )
        except Exception as exc:  # noqa: BLE001 - 读取失败降级为空概览
            logger.warning("成本时序读取失败（降级为空）：%s", exc)
            return summary

        for row in rows:
            value = int(float(row.get("value", 0) or 0))
            summary.total_micro_usd += value
            bl = str(row.get("biz_line_id"))
            model = str(row.get("model") or "")
            summary.by_biz_line[bl] = summary.by_biz_line.get(bl, 0) + value
            summary.by_model[model] = summary.by_model.get(model, 0) + value
        summary.runs = len(rows)
        return summary


def _window_of(payload: dict[str, Any]) -> tuple[datetime, datetime]:
    """解析时间窗（默认近 30 天，UTC naive）。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    until = payload.get("until")
    since = payload.get("since")
    until_dt = until if isinstance(until, datetime) else now
    if isinstance(since, datetime):
        since_dt = since
    else:
        since_dt = until_dt - timedelta(days=int(payload.get("days", 30)))
    return since_dt, until_dt
