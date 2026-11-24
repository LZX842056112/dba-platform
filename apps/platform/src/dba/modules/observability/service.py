"""模块 03 · 对外只读服务（``ObservabilityService``）。

对齐《设计方案 v2》§6.4 / §7.4 / §5.9 归属矩阵 与《实现要点清单》§4.0。

★ 为什么要有这一层
------------------
§4.0 归属矩阵规定：``app_agent`` / ``alert_event`` / ``metric_daily`` / ``anomaly_report``
的 owner 是 **模块 03 observability**。因此：

* API 层（``/obs/*``）与模块 10（FinOps 成本大屏）**都只能经本服务**读这些对象，
  **不得**直接 SQL 打表（尤其 ``metric_daily``——10 也必须走 ``timeseries``）；
* 本服务是 owner 暴露的读取接口，内部才去调 L5 Repository。

★ ``self_cost``（P1-4）：平台自身（03/10）消耗走**独立视图**，不进业务成本曲线；
  这里直接按 ``module ∈ {observability, finops}`` 过滤 ``run`` 汇总。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .anomaly import SELF_MODULES

__all__ = ["ObservabilityService"]

logger = logging.getLogger("dba.modules.observability.service")


class ObservabilityService:
    """观测域只读服务（owner 读取接口）。"""

    def __init__(
        self,
        *,
        alert_repo: Any = None,
        metric_repo: Any = None,
        agent_repo: Any = None,
        run_repo: Any = None,
        report_repo: Any = None,
        skill_metrics: Any = None,
        memory_metrics: Any = None,
        topology: Any = None,
    ) -> None:
        self._alerts = alert_repo
        self._metric = metric_repo
        self._agents = agent_repo
        self._run = run_repo
        self._reports = report_repo
        self._skill_metrics = skill_metrics
        self._memory_metrics = memory_metrics
        self._topology = topology

    # ── 指标时序（★ 10 的成本大屏也必须走这里，不直连表）──────────────
    async def timeseries(
        self,
        *,
        metric: str,
        since: datetime | Any,
        until: datetime | Any,
        biz_line_id: int | None = None,
        agent_uid: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """读 ``metric_daily`` 时序（返回 ``[{ts, value, ...}]``）。"""
        if self._metric is None:
            return []
        flt: dict[str, Any] = {"since": since, "until": until, "biz_line_id": biz_line_id}
        if model:
            flt["model"] = model
        try:
            rows = await self._metric.timeseries(flt)
        except Exception as exc:  # noqa: BLE001 - 查询失败降级为空
            logger.warning("metric_daily 时序查询失败（降级为空）：%s", exc)
            return []
        series: list[dict[str, Any]] = []
        for row in rows:
            if agent_uid is not None and str(row.get("agent_uid") or "") != agent_uid:
                continue
            series.append(
                {
                    "ts": _iso(row.get("stat_date")),
                    "value": _num(row.get(metric)),
                    "agent_uid": row.get("agent_uid"),
                    "model": row.get("model"),
                    "biz_line_id": row.get("biz_line_id"),
                }
            )
        return series

    # ── 异常 ────────────────────────────────────────────────────────
    async def anomalies(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """异常事件列表（MySQL ``alert_event``）。"""
        if self._alerts is None:
            return []
        flt = {"status": status, "category": category, "severity": severity, "limit": limit}
        try:
            return [dict(r) for r in await self._alerts.list_events(flt)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("异常列表查询失败（降级为空）：%s", exc)
            return []

    async def anomaly_detail(self, alert_id: int) -> dict[str, Any] | None:
        """``alert_event`` + （权威）``anomaly_report``（U14）。"""
        event: dict[str, Any] | None = None
        if self._alerts is not None:
            try:
                row = await self._alerts.get(alert_id)
                event = dict(row) if row is not None else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("alert_event 查询失败（降级为 null）：%s", exc)
        report: dict[str, Any] | None = None
        if self._reports is not None:
            try:
                report = await self._reports.get_by_alert(alert_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("anomaly_report 查询失败（降级为 null）：%s", exc)
        if event is None and report is None:
            return None
        return {"alert_event": event, "anomaly_report": report}

    # ── Agent 心跳 ──────────────────────────────────────────────────
    async def agents(
        self, *, biz_line_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Agent 注册列表。"""
        if self._agents is None:
            return []
        try:
            return [
                dict(r)
                for r in await self._agents.list_agents(
                    {"biz_line_id": biz_line_id, "status": status}
                )
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent 列表查询失败（降级为空）：%s", exc)
            return []

    async def agent_detail(self, agent_uid: str) -> dict[str, Any]:
        """``GET /obs/agents/{uid}``：Agent 元数据 + 最近 run + 汇总指标。"""
        if self._agents is None:
            return {}
        try:
            meta = dict(await self._agents.by_uid(agent_uid) or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent 详情查询失败（降级为空）：%s", exc)
            return {}
        if not meta:
            return {}
        runs = await self._runs_of({"agent_uid": agent_uid}, limit=20)
        success = sum(1 for r in runs if str(r.get("status")) == "success")
        cost = sum(int(r.get("cost_micro_usd") or 0) for r in runs)
        return {
            "meta": meta,
            "metrics": {
                "recent_runs": len(runs),
                "success_rate": round(success / len(runs), 4) if runs else 0.0,
                "cost_micro_usd": cost,
            },
            "skills": [],  # 无 agent→skill 直连映射（skill 按 biz_line 归属），诚实空态
            "recent_runs": runs,
        }

    # ── 技能 / 记忆指标 ─────────────────────────────────────────────
    async def skill_metrics(self, stat_date: Any, biz_line_id: int | None) -> Any:
        if self._skill_metrics is None:
            return None
        return await self._skill_metrics.compute(stat_date, biz_line_id)

    async def memory_metrics(self, stat_date: Any, biz_line_id: int | None) -> Any:
        if self._memory_metrics is None:
            return None
        return await self._memory_metrics.compute(stat_date, biz_line_id)

    # ── ★ 平台自身消耗（P1-4，独立视图）─────────────────────────────
    async def self_cost(self, *, since: datetime, until: datetime) -> dict[str, Any]:
        """平台自身（03/10）消耗，**不进业务成本曲线**（§7.6 ``GET /obs/self-cost``）。"""
        by_module: dict[str, dict[str, int]] = {}
        total_cost = 0
        total_runs = 0
        # ★ 一次 GROUP BY 取代原「每个模块各拉 ≤1 万行再 sum」。
        for row in await self._totals_by_module(since=since, until=until):
            module = str(row.get("module") or "")
            if module not in SELF_MODULES:
                continue
            runs = int(row.get("run_count") or 0)
            cost = int(row.get("cost_micro_usd") or 0)
            by_module[module] = {"cost_micro_usd": cost, "runs": runs}
            total_cost += cost
            total_runs += runs
        # 窗口内没有记录的模块不补 0：前端据此显示「没有平台自身运行记录」的诚实空态
        return {
            "by_module": by_module,
            "total_cost_micro_usd": total_cost,
            "total_runs": total_runs,
            "note": "平台自身消耗（observability/finops），已从业务成本曲线中排除",
        }

    async def _aggregate_runs(
        self, *, since: datetime, until: datetime, biz_line_id: int | None
    ) -> list[dict[str, Any]]:
        """按 (业务线, Agent) 聚合 Run；读取失败降级为空，不抛异常。"""
        if self._run is None:
            return []
        try:
            rows: list[dict[str, Any]] = await self._run.aggregate_daily(
                since, until, biz_line_id=biz_line_id, exclude_modules=()
            )
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 run 聚合失败（降级）：%s", exc)
            return []

    async def _totals_by_module(self, *, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """按模块聚合 Run 数与成本；读取失败降级为空。"""
        if self._run is None:
            return []
        try:
            rows: list[dict[str, Any]] = await self._run.totals_by_module(since, until)
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 run 模块汇总失败（降级）：%s", exc)
            return []

    async def _runs_of(self, flt: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        """读取 run 明细（降级为空，不抛异常）。

        ★ ``limit`` 必填：这里只服务「详情/样本」类需求（如 Agent 详情页最近 20 条）。
          统计类场景请走 ``_aggregate_runs`` / ``_totals_by_module``，
          不要再用「拉 1 万行回 Python 数一遍」。
        """
        if self._run is None:
            return []
        try:
            rows: list[dict[str, Any]] = await self._run.list_runs(flt, limit=limit)
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 run 明细失败（降级）：%s", exc)
            return []

    # ── 概览 / 拓扑 ─────────────────────────────────────────────────
    async def overview(
        self, *, since: datetime, until: datetime, biz_line_id: int | None = None
    ) -> dict[str, Any]:
        """返回区间内的运行指标、成本趋势和 Agent 排行。

        ★ 计数走 ``aggregate_daily``（SQL 侧 GROUP BY），不再把明细行拉回 Python。
        """
        rows = await self.timeseries(
            metric="cost_micro_usd", since=since, until=until, biz_line_id=biz_line_id
        )
        cost_total = sum(int(r["value"]) for r in rows)
        groups = await self._aggregate_runs(since=since, until=until, biz_line_id=biz_line_id)
        total = sum(int(g.get("run_count") or 0) for g in groups)
        success = sum(int(g.get("success_count") or 0) for g in groups)
        running = sum(int(g.get("running_count") or 0) for g in groups)
        # 静默失败的判据在 SQL 里（repo.aggregate_daily），此处只取汇总值
        silent = sum(int(g.get("silent_fail_count") or 0) for g in groups)
        success_rate = (success / total) if total else 0.0
        return {
            "kpi_cards": {
                "total_runs": {"value": total, "label": "区间内运行"},
                "running_now": {"value": running, "label": "当前运行中 Run"},
                "cost_today_micro": {"value": cost_total},
                "success_rate": {"value": round(success_rate, 4)},
                "silent_failures": {"value": silent, "label": "静默失败（零产出，窗口内）"},
            },
            "trend": {"granularity": "day", "series": rows},
            "top_agents": _top_agents(groups),
        }

    async def topology(self, biz_line_id: int | None) -> dict[str, Any]:
        """``GET /obs/topology``。"""
        if self._topology is None:
            return {"nodes": [], "edges": []}
        graph = await self._topology.build(biz_line_id)
        return dict(graph.as_dict())


def _top_agents(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 agent_uid 汇总已聚合的分组（top 5）。

    输入是 ``aggregate_daily`` 的分组行（已按 业务线 × Agent 聚合），因此这里只需
    跨业务线再合并一次，不再扫描明细。
    """
    agg: dict[str, dict[str, int]] = {}
    for group in groups:
        uid = str(group.get("agent_uid") or "")
        if not uid:
            continue
        bucket = agg.setdefault(uid, {"runs": 0, "success": 0})
        bucket["runs"] += int(group.get("run_count") or 0)
        bucket["success"] += int(group.get("success_count") or 0)
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["runs"], reverse=True)[:5]
    return [
        {
            "agent_uid": uid,
            "runs": data["runs"],
            "success_rate": round(data["success"] / data["runs"], 4) if data["runs"] else 0.0,
        }
        for uid, data in ranked
    ]


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
