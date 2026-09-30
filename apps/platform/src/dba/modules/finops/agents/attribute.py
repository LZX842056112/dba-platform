"""模块 10 · 四角色 Agent 之二：归因（attribute）。

对齐《设计方案 v2》§6.5 与《实现要点清单》§3.25、§7.5 ``/finops/cost/attribution``。

职责：**四维归因**（业务线 / Agent / 模型 / 技能）与**价格修正后重算**。

★ 为什么「重算」是设计收益而不是补丁
-----------------------------------
成本被当作**派生量**而不是事实量：明细里固化的是 **token 数与 ``price_book_id``**，
而不是最终金额。所以价格表修正后可以**精确重算**历史成本——这是 §6.5 的核心设计收益。

★ 归属：本模块不直连 ``llm_call``（owner=capabilities.telemetry）。明细经注入的
``detail_source`` 取得（由 DI 用允许的 owner 读接口构造）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from dba_runtime import AgentOutput, RunContext, traced

__all__ = ["Attribution", "RecomputeReport", "CostAttributor", "AttributeAgent"]

logger = logging.getLogger("dba.modules.finops.attribute")


@dataclass(slots=True)
class Attribution:
    """四维归因结果（``breakdown`` 是逐条贡献明细）。"""

    trace_id: str
    biz_line: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    skill: dict[str, Any] = field(default_factory=dict)
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    total_cost_micro_usd: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "biz_line": self.biz_line,
            "agent": self.agent,
            "model": self.model,
            "skill": self.skill,
            "breakdown": self.breakdown,
            "total_cost_micro_usd": self.total_cost_micro_usd,
        }


@dataclass(slots=True)
class RecomputeReport:
    """重算报告（价格表变更后调用）。"""

    start: date
    end: date
    scanned: int = 0
    recomputed: int = 0
    skipped_unpriced: int = 0
    delta_micro_usd: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "scanned": self.scanned,
            "recomputed": self.recomputed,
            "skipped_unpriced": self.skipped_unpriced,
            "delta_micro_usd": self.delta_micro_usd,
            "note": self.note,
        }


class CostAttributor:
    """四维成本归因 + 价格修正后重算。"""

    def __init__(
        self,
        *,
        detail_source: Any = None,
        cost_normalizer: Any = None,
    ) -> None:
        #: ``detail_source.by_trace(trace_id)`` / ``.range(start, end)``
        self._details = detail_source
        self._cost = cost_normalizer

    async def attribute(self, trace_id: str) -> Attribution:
        """按 trace_id 取明细并做四维归因。"""
        rows = await self._rows_for_trace(trace_id)
        return self._aggregate(trace_id, rows)

    @staticmethod
    def _aggregate(trace_id: str, rows: list[dict[str, Any]]) -> Attribution:
        """把明细行聚合成四维归因（纯逻辑，便于单测）。"""
        result = Attribution(trace_id=trace_id)
        totals: dict[str, dict[str, int]] = {"biz_line": {}, "agent": {}, "model": {}, "skill": {}}
        total = 0
        for row in rows:
            cost = int(row.get("cost_micro_usd") or 0)
            total += cost
            dims = {
                "biz_line": str(row.get("biz_line_id") or ""),
                "agent": str(row.get("agent_uid") or ""),
                "model": str(row.get("model") or ""),
                "skill": str(row.get("skill_key") or ""),
            }
            for dim, key in dims.items():
                totals[dim][key] = totals[dim].get(key, 0) + cost
            result.breakdown.append(
                {
                    "biz_line_id": row.get("biz_line_id"),
                    "agent_uid": row.get("agent_uid"),
                    "model": row.get("model"),
                    "skill_key": row.get("skill_key"),
                    "cost_micro_usd": cost,
                }
            )

        result.total_cost_micro_usd = total
        denom = total or 1
        result.biz_line = _top(totals["biz_line"], denom, "biz_line_id", total)
        result.agent = _top(totals["agent"], denom, "agent_uid", total)
        result.model = _top(totals["model"], denom, "model", total)
        result.skill = _top(totals["skill"], denom, "skill_key", total)
        return result

    async def recompute(self, start: date, end: date) -> RecomputeReport:
        """★ 价格表变更后重算历史成本（成本是派生量，可精确重算）。"""
        report = RecomputeReport(start=start, end=end)
        rows = await self._rows_for_range(start, end)
        report.scanned = len(rows)
        if self._cost is None:
            report.note = "未注入 CostNormalizer，无法重算（仅统计）"
            return report
        for row in rows:
            normalized = self._cost.normalize(row)
            if getattr(normalized, "unknown_price", False):
                report.skipped_unpriced += 1
                continue
            new_total = int(normalized.total_micro_usd)
            old_total = int(row.get("cost_micro_usd") or 0)
            report.delta_micro_usd += new_total - old_total
            report.recomputed += 1
        report.note = "重算仅统计差额；明细回写由 worker 的 recompute job 执行（见报告遗留问题）"
        return report

    async def _rows_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        if self._details is None:
            return []
        try:
            return list(await self._details.by_trace(trace_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("归因明细读取失败（降级为空）：%s", exc)
            return []

    async def _rows_for_range(self, start: date, end: date) -> list[dict[str, Any]]:
        if self._details is None:
            return []
        try:
            return list(await self._details.range(start, end))
        except Exception as exc:  # noqa: BLE001
            logger.warning("重算明细读取失败（降级为空）：%s", exc)
            return []


def _top(counts: dict[str, int], denom: int, key_name: str, total: int) -> dict[str, Any]:
    """取贡献最大的维度项（含 share）。"""
    if not counts:
        return {}
    key = max(counts, key=lambda k: counts[k])
    return {
        key_name: key,
        "cost_micro_usd": counts[key],
        "share": round(counts[key] / denom, 4) if denom else 0.0,
        "total_micro_usd": total,
    }


class AttributeAgent:
    """归因 Agent：按 trace_id 产出四维归因。"""

    name = "attribute"

    def __init__(self, attributor: CostAttributor) -> None:
        self._attributor = attributor

    @traced("step.finops.attribute", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        trace_id = str(payload.get("trace_id") or ctx.trace_id)
        attribution = await self._attributor.attribute(trace_id)
        return AgentOutput(
            data={"attribution": attribution.as_dict()},
            confidence=1.0,
            meta={"agent": self.name},
        )
