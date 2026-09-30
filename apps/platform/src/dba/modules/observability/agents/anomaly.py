"""模块 03 · 四角色 Agent 之三：异常扫描（anomaly）。

对齐《设计方案 v2》§6.4、§5.4.6 与《实现要点清单》§3.19、U14、§12.5（可验证产出②）。

职责：跑无阈值检测（``AnomalyDetector``）与静默失败检测（``SilentFailureDetector``），
把每条异常写成 **MySQL ``alert_event``** + **Mongo ``anomaly_report``**。

★ U14 双写分工（照抄 v1 只写一份会丢证据）
------------------------------------------
* MySQL ``alert_event``：**可检索的事件行**；``attribution_json`` 只存**展示摘要**
  （``Attribution.display_summary()``），因为它要出现在列表接口里、不宜过大；
* Mongo ``anomaly_report``：**归因的权威源**，存完整 ``attribution``（含 hypothesis、
  各维度贡献、skill reruns 明细）与 ``evidence[]``。

★ 无阈值：本 Agent **不读任何阈值配置**，触发条件完全来自检测器的稳健偏差（DoD#2）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dba_runtime import AgentOutput, RunContext, traced

from ..anomaly import Anomaly, AnomalyDetector, Scope, SilentFailureDetector, Window

__all__ = ["AnomalyScanAgent"]

logger = logging.getLogger("dba.modules.observability.anomaly_agent")


class AnomalyScanAgent:
    """异常扫描 Agent：检测 → 写 ``alert_event``（MySQL）+ ``anomaly_report``（Mongo）。"""

    name = "anomaly"

    def __init__(
        self,
        *,
        detector: AnomalyDetector,
        silent_detector: SilentFailureDetector | None = None,
        alert_repo: Any = None,
        report_repo: Any = None,
    ) -> None:
        self._detector = detector
        self._silent = silent_detector
        self._alerts = alert_repo
        self._reports = report_repo

    @traced("step.obs.anomaly", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        window = _window_of(payload)
        scope = _scope_of(payload, ctx)

        anomalies = await self._detector.detect(window, scope)
        if self._silent is not None:
            anomalies.extend(await self._silent.detect(window))

        persisted: list[dict[str, Any]] = []
        for anomaly in anomalies:
            alert_id = await self._persist(anomaly)
            persisted.append(
                {
                    "alert_id": alert_id,
                    "category": anomaly.category,
                    "severity": anomaly.severity,
                    "metric": anomaly.metric,
                    "robust_zscore": anomaly.robust_zscore,
                }
            )

        return AgentOutput(
            data={
                "alerts": persisted,
                "alert_count": len(persisted),
                "window": {"since": window.since.isoformat(), "until": window.until.isoformat()},
            },
            confidence=1.0,
            meta={"agent": self.name},
        )

    async def _persist(self, anomaly: Anomaly) -> int | None:
        """写一条异常：先写 MySQL（拿到 alert_id），再写 Mongo（U14 权威归因）。"""
        alert_id: int | None = None
        if self._alerts is not None:
            row: dict[str, Any] = {
                "severity": anomaly.severity,
                "category": anomaly.category,
                "scope_type": anomaly.scope.type,
                "scope_id": anomaly.scope.id,
                "metric": anomaly.metric,
                "observed_value": anomaly.observed,
                "baseline_value": anomaly.baseline,
                "robust_zscore": anomaly.robust_zscore,
                # ★ 只存展示摘要；完整归因在 Mongo（U14）
                "attribution_json": anomaly.attribution.display_summary(),
                "suggestion_json": _suggestion_for(anomaly),
                "trace_id": anomaly.trace_id,
                "status": "open",
            }
            try:
                alert_id = int(await self._alerts.insert(row))
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余（继续扫描）
                logger.warning("写 alert_event 失败（跳过该条）：%s", exc)

        if self._reports is not None:
            doc: dict[str, Any] = {
                "alert_id": alert_id,
                "detected_at": datetime.now(UTC).replace(tzinfo=None),
                "category": anomaly.category,
                "scope": {"type": anomaly.scope.type, "id": anomaly.scope.id},
                "severity": anomaly.severity,
                "evidence": [anomaly.evidence()],
                "attribution": anomaly.attribution.as_doc(),  # ★ 权威全文
                "suggestion": _suggestion_for(anomaly),
                "trace_id": anomaly.trace_id,
                "status": "open",
            }
            try:
                await self._reports.save(doc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("写 anomaly_report 失败（跳过该条）：%s", exc)
        return alert_id


def _suggestion_for(anomaly: Anomaly) -> dict[str, Any]:
    """按类别给出建议动作（默认**只建议、不自动执行**——与护栏保守原则一致）。"""
    if anomaly.category == "cost_spike":
        return {
            "action": "downgrade_and_throttle",
            "expected_saving_micro_usd_per_day": max(0, int(anomaly.observed - anomaly.baseline)),
            "risk": "low",
            "auto_applicable": False,
        }
    if anomaly.category == "silent_failure":
        return {"action": "inspect_run", "risk": "low", "auto_applicable": False}
    if anomaly.category == "skill_rot":
        return {"action": "review_or_retire_skill", "risk": "medium", "auto_applicable": False}
    if anomaly.category == "loop_suspect":
        return {"action": "throttle_agent", "risk": "medium", "auto_applicable": False}
    return {"action": "notify_owner", "risk": "low", "auto_applicable": False}


def _window_of(payload: dict[str, Any]) -> Window:
    """从 payload 解析时间窗（默认近 24h，UTC）。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    until = payload.get("until")
    since = payload.get("since")
    until_dt = until if isinstance(until, datetime) else now
    if isinstance(since, datetime):
        since_dt = since
    else:
        since_dt = until_dt - timedelta(hours=int(payload.get("hours", 24)))
    return Window(since=since_dt, until=until_dt)


def _scope_of(payload: dict[str, Any], ctx: RunContext) -> Scope:
    """异常归属范围（优先 payload.agent_uid，其次 biz_line，最后 GLOBAL）。"""
    agent_uid = payload.get("agent_uid")
    if agent_uid:
        return Scope("AGENT", str(agent_uid))
    biz_line_id = payload.get("biz_line_id", ctx.biz_line_id)
    if biz_line_id is not None:
        return Scope("BIZ_LINE", str(biz_line_id))
    return Scope("GLOBAL")
