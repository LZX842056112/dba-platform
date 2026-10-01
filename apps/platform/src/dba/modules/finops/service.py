"""模块 10 · 对外只读服务（``FinopsService``）。

对齐《设计方案 v2》§6.5 / §7.5 / §5.9 归属矩阵 与《实现要点清单》§4.0、U26。

★ 归属铁律（§5.9）
------------------
* 成本时序（``metric_daily``）**必须**经 ``ObservabilityService.timeseries`` 读，
  **禁止**直接 SQL 打表；
* ``budget`` / ``budget_usage`` 只经 L4 ``BudgetGuard`` 的只读接口；
* ``finops_recommendation`` 的 owner 是本模块，可直接读写。

★ U26 kill_switch 持久化（已登记的**文档缺失**）
----------------------------------------------
v2 没有为 kill_switch 设计存储表。本批次做**最小落地**：进程内策略覆盖 + 返回
``effective_at``（并在具备 Redis 广播后同步到其它副本）。真正的「多副本一致 + 审计留痕」
需要一张 ``guardrail_policy`` 表，登记为报告「遗留问题（文档缺失）」。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .guardrail import GuardrailPolicy

__all__ = ["FinopsService"]

logger = logging.getLogger("dba.modules.finops.service")


class FinopsService:
    """FinOps 域只读（+ 少量治理写）服务。"""

    def __init__(
        self,
        *,
        observability: Any = None,
        attributor: Any = None,
        curve: Any = None,
        reco_repo: Any = None,
        coverage_source: Any = None,
        budget: Any = None,
        policy: GuardrailPolicy | None = None,
    ) -> None:
        self._obs = observability
        self._attributor = attributor
        self._curve = curve
        self._reco = reco_repo
        self._coverage = coverage_source
        self._budget = budget
        self._policy = policy or GuardrailPolicy()

    # ── 成本概览 / 时序 ─────────────────────────────────────────────
    async def cost_summary(
        self, *, since: datetime, until: datetime, biz_line_id: int | None = None
    ) -> dict[str, Any]:
        """``GET /finops/cost/summary``（总量 + by_module/by_biz_line/by_model）。"""
        rows = await self._timeseries("cost_micro_usd", since, until, biz_line_id)
        total = sum(int(r["value"]) for r in rows)
        by_biz_line: dict[str, int] = {}
        by_model: dict[str, int] = {}
        for row in rows:
            by_biz_line[str(row.get("biz_line_id"))] = by_biz_line.get(
                str(row.get("biz_line_id")), 0
            ) + int(row["value"])
            model = str(row.get("model") or "")
            by_model[model] = by_model.get(model, 0) + int(row["value"])
        return {
            "total": total,
            "by_module": {},  # 业务成本口径不含平台自身（平台自身见 /obs/self-cost）
            "by_biz_line": by_biz_line,
            "by_model": by_model,
        }

    async def timeseries(
        self,
        *,
        group_by: str,
        granularity: str,
        since: datetime,
        until: datetime,
        biz_line_id: int | None = None,
    ) -> dict[str, Any]:
        """``GET /finops/cost/timeseries``。"""
        _ = granularity
        rows = await self._timeseries("cost_micro_usd", since, until, biz_line_id)
        series: list[dict[str, Any]] = []
        for row in rows:
            series.append(
                {
                    "ts": row.get("ts"),
                    "cost_micro_usd": int(row["value"]),
                    "group_by": group_by,
                    "group": _group_key(row, group_by),
                }
            )
        return {"series": series}

    async def attribution(self, trace_id: str) -> dict[str, Any]:
        """``GET /finops/cost/attribution``。"""
        if self._attributor is None:
            return {"trace_id": trace_id, "breakdown": []}
        result = await self._attributor.attribute(trace_id)
        return dict(result.as_dict())

    async def top_spenders(
        self, *, dimension: str, period: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """``GET /finops/cost/top-spenders``。"""
        _ = period
        since, until = _default_window(days=30)
        rows = await self._timeseries("cost_micro_usd", since, until, None)
        agg: dict[str, int] = {}
        for row in rows:
            key = _group_key(row, dimension)
            agg[key] = agg.get(key, 0) + int(row["value"])
        ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [{"key": key, "cost_micro_usd": value} for key, value in ranked]

    async def cache_stats(
        self, *, since: datetime, until: datetime, biz_line_id: int | None = None
    ) -> dict[str, Any]:
        """``GET /finops/cache/stats``（复用 observability 的记忆指标）。"""
        _ = (since, until)
        metrics = None
        if self._obs is not None:
            metrics = await self._obs.memory_metrics(since.date(), biz_line_id)
        return {
            "hit_rate": getattr(metrics, "hit_rate", 0.0) if metrics else 0.0,
            "saved_micro_usd": 0,  # 精确节省需 llm_call 明细回填（见报告遗留问题）
            "by_task": [],
        }

    async def coverage(
        self, *, since: datetime, until: datetime, biz_line_id: int | None = None
    ) -> dict[str, Any]:
        """``GET /finops/cost/coverage``——计价覆盖率，「成本可信」的第一证据。"""
        if self._coverage is None:
            return {
                "priced_ratio": 0.0,
                "unpriced_calls": 0,
                "by_provider": [],
                "note": "未接入计价明细源（需 llm_call 明细），覆盖率暂不可用",
            }
        try:
            return dict(await self._coverage(since, until, biz_line_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("计价覆盖率计算失败（降级）：%s", exc)
            return {"priced_ratio": 0.0, "unpriced_calls": 0, "by_provider": []}

    async def curve(self, *, biz_line_id: int, days: int = 30) -> dict[str, Any]:
        """``GET /finops/curve/reuse-vs-token``（带对照组 + 相关系数）。"""
        if self._curve is None:
            return {"biz_line_id": biz_line_id, "days": days, "note": "曲线分析器未装配"}
        data = await self._curve.reuse_vs_token(biz_line_id, days)
        return dict(data.as_dict())

    # ── 建议（owner=finops）─────────────────────────────────────────
    async def recommendations(
        self, *, scope_type: str, scope_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        if self._reco is None:
            return []
        try:
            rows = await self._reco.list(
                {"scope_type": scope_type, "scope_id": scope_id, "status": status}
            )
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("建议列表查询失败（降级为空）：%s", exc)
            return []

    async def apply_recommendation(self, reco_id: str, *, dry_run: bool = True) -> dict[str, Any]:
        """``POST /finops/recommendations/{id}/apply``。默认 ``dry_run``（不落状态）。"""
        if dry_run:
            return {
                "ok": True,
                "impact": "dry_run",
                "rollback_token": f"rb_{reco_id}",
            }
        if self._reco is not None:
            try:
                await self._reco.set_status(reco_id, "applied")
            except Exception as exc:  # noqa: BLE001
                logger.warning("建议状态更新失败：%s", exc)
        return {"ok": True, "impact": "applied", "rollback_token": f"rb_{reco_id}"}

    async def reject_recommendation(self, reco_id: str, reason: str) -> dict[str, Any]:
        if self._reco is not None:
            try:
                await self._reco.set_status(reco_id, "rejected")
            except Exception as exc:  # noqa: BLE001
                logger.warning("建议状态更新失败：%s", exc)
        return {"ok": True, "reason": reason}

    # ── 预算用量（只读，经 L4 BudgetGuard）──────────────────────────
    async def budget_usage(self, budget_id: int, period_start: Any | None = None) -> dict[str, Any]:
        """``GET /finops/budgets/{id}/usage``。

        ★ 只读预算用量经 L4 ``BudgetService``（owner=capabilities.budget）：按 id 取生效预算
        → ``snapshot`` 读权威用量快照，返回真实 ``consumed/reserved/remaining``。
        """
        _ = period_start
        empty = {"consumed": 0, "reserved": 0, "remaining": 0, "breaker_state": "CLOSED"}
        if self._budget is None:
            return empty
        try:
            effective = await self._budget.resolve_by_id(budget_id)
        except Exception as exc:  # noqa: BLE001 - 只读端点，失败降级为诚实空态
            logger.warning("预算解析失败（降级为空态）：%s", exc)
            return empty
        if effective is None:
            return {**empty, "note": "预算不存在"}
        try:
            snap = await self._budget.snapshot(effective)
        except Exception as exc:  # noqa: BLE001
            logger.warning("预算快照失败（降级为空态）：%s", exc)
            return empty
        consumed = int(snap["consumed_micro_usd"])
        reserved = int(snap["reserved_micro_usd"])
        return {
            "consumed": consumed,
            "reserved": reserved,
            "remaining": max(0, effective.amount_micro_usd - consumed - reserved),
            "breaker_state": str(snap.get("breaker_state", "CLOSED")),
            "amount_micro_usd": effective.amount_micro_usd,
            "used_pct": int(snap.get("used_pct", 0)),
        }

    # ── 护栏策略（U26：进程内最小实现）─────────────────────────────
    async def guardrail_policy(self) -> dict[str, Any]:
        return _policy_dict(self._policy)

    async def update_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        """``PATCH /finops/guardrail/policy``（进程内覆盖；持久化见报告遗留问题）。"""
        allowed = {
            "enabled",
            "allow_downgrade",
            "allow_compress",
            "allow_rate_limit",
            "allow_circuit_break",
            "exemption_priority",
            "breaker_window_s",
            "breaker_consecutive_windows",
            "breaker_cooldown_s",
            "max_downgrades_per_run",
            "kill_switch",
        }
        changes = {k: v for k, v in patch.items() if k in allowed}
        if changes:
            self._policy = replace(self._policy, **changes)
        return _policy_dict(self._policy)

    async def kill_switch(self, *, enabled: bool, reason: str) -> dict[str, Any]:
        """``POST /finops/guardrail/kill-switch``（★ 全局逃生开关）。"""
        self._policy = replace(self._policy, kill_switch=enabled)
        effective_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        logger.warning("kill_switch 切换 enabled=%s reason=%s", enabled, reason)
        return {
            "ok": True,
            "effective_at": effective_at,
            "persisted": False,  # ★ U26：无持久化表（文档缺失），仅进程内生效
            "note": "U26：kill_switch 无持久化表，进程内生效；多副本一致待补",
        }

    async def loop_alerts(self, *, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """``GET /finops/loop-alerts``。

        读 observability 的 ``alert_event``（``category=loop_suspect``）。
        """
        if self._obs is None:
            return []
        alerts: list[dict[str, Any]] = await self._obs.anomalies(category="loop_suspect", limit=200)
        return alerts

    # ── 内部 ────────────────────────────────────────────────────────
    async def _timeseries(
        self, metric: str, since: datetime, until: datetime, biz_line_id: int | None
    ) -> list[dict[str, Any]]:
        """★ 成本时序经 ObservabilityService（owner=observability），不直连 metric_daily。"""
        if self._obs is None:
            return []
        rows: list[dict[str, Any]] = await self._obs.timeseries(
            metric=metric, since=since, until=until, biz_line_id=biz_line_id
        )
        return rows


def _group_key(row: dict[str, Any], group_by: str) -> str:
    if group_by in ("biz_line", "biz_line_id"):
        return str(row.get("biz_line_id"))
    if group_by in ("agent", "agent_uid"):
        return str(row.get("agent_uid") or "")
    if group_by == "model":
        return str(row.get("model") or "")
    return str(row.get("model") or "")


def _policy_dict(policy: GuardrailPolicy) -> dict[str, Any]:
    return {
        "enabled": policy.enabled,
        "allow_downgrade": policy.allow_downgrade,
        "allow_compress": policy.allow_compress,
        "allow_rate_limit": policy.allow_rate_limit,
        "allow_circuit_break": policy.allow_circuit_break,
        "exemption_priority": policy.exemption_priority,
        "breaker_window_s": policy.breaker_window_s,
        "breaker_consecutive_windows": policy.breaker_consecutive_windows,
        "breaker_cooldown_s": policy.breaker_cooldown_s,
        "max_downgrades_per_run": policy.max_downgrades_per_run,
        "kill_switch": policy.kill_switch,
    }


def _default_window(days: int = 30) -> tuple[datetime, datetime]:
    until = datetime.now(UTC).replace(tzinfo=None)
    return until - timedelta(days=days), until
