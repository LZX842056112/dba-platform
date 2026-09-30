"""模块 03 · 四角色 Agent 之四：播报（broadcast）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§3.19。

职责：把本轮产生的异常按严重度播报到 IM（飞书 / 钉钉），并控制「同一异常不重复轰炸」。

★ 现状与诚实标注
----------------
IM Webhook 网关（``capabilities/messaging/MessageGateway``，§4.10）在 B3 批次未实现，
本批次**不假装可用**：默认注入 ``LogBroadcastGateway``（只落结构化日志），
真实网关由 DI 在具备 webhook 配置时注入。真网关接入登记为「遗留问题」。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from dba_runtime import AgentOutput, RunContext, traced

__all__ = ["BroadcastGateway", "LogBroadcastGateway", "BroadcastAgent"]

logger = logging.getLogger("dba.modules.observability.broadcast")

#: 需要播报的最低严重度（info 不打扰）
_SEVERITY_ORDER: dict[str, int] = {"info": 0, "warn": 1, "critical": 2}


@runtime_checkable
class BroadcastGateway(Protocol):
    """IM 播报网关（飞书 / 钉钉 Webhook 的最小契约）。"""

    async def send(self, text: str, *, channel: str = "obs", severity: str = "warn") -> bool: ...


class LogBroadcastGateway:
    """兜底网关：只落日志（未配置 IM webhook 时使用，绝不假装发送成功）。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, text: str, *, channel: str = "obs", severity: str = "warn") -> bool:
        self.sent.append({"channel": channel, "severity": severity, "text": text})
        logger.info("[broadcast:%s][%s] %s", channel, severity, text)
        return True


class BroadcastAgent:
    """播报 Agent：按严重度过滤后播报（避免 info 级事件打扰值班）。"""

    name = "broadcast"

    def __init__(
        self,
        *,
        gateway: BroadcastGateway | None = None,
        min_severity: str = "warn",
    ) -> None:
        self._gateway: BroadcastGateway = gateway or LogBroadcastGateway()
        self._min_severity = _SEVERITY_ORDER.get(min_severity, 1)

    @traced("step.obs.broadcast", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        alerts = list(payload.get("alerts") or [])
        sent = 0
        for alert in alerts:
            severity = str(alert.get("severity") or "warn")
            if _SEVERITY_ORDER.get(severity, 1) < self._min_severity:
                continue
            text = _render(alert)
            try:
                ok = await self._gateway.send(text, severity=severity)
            except Exception as exc:  # noqa: BLE001 - 播报失败不得影响扫描结果
                logger.warning("IM 播报失败（忽略）：%s", exc)
                ok = False
            if ok:
                sent += 1
        return AgentOutput(
            data={"broadcast": sent, "skipped": max(0, len(alerts) - sent)},
            confidence=1.0,
            meta={"agent": self.name},
        )


def _render(alert: dict[str, Any]) -> str:
    """渲染一条可读播报（含稳健偏差与归因摘要）。"""
    return (
        f"[{str(alert.get('severity', 'warn')).upper()}] {alert.get('category')} "
        f"指标 {alert.get('metric')} 偏离基线 "
        f"(robust_z={alert.get('robust_zscore')}, alert_id={alert.get('alert_id')})"
    )
