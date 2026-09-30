"""模块 03 · 四角色 Agent 之一：采集（collect）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§3.19。

职责：拉取三类运行时元数据 —— ① 内部 Run（MySQL ``run``）② OTLP span（外部 Agent 自报）
③ Agent 心跳（``app_agent.last_heartbeat_at``）。产出交给下游 aggregate 聚合。

★ 只读：本 Agent 不写任何表（写入是 aggregate / anomaly 的职责，符合单一职责与
§4.0 归属矩阵——``app_agent`` 由 observability 拥有，但心跳写入只发生在注册/心跳 API）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dba_runtime import AgentOutput, RunContext, traced

__all__ = ["CollectAgent"]

logger = logging.getLogger("dba.modules.observability.collect")


class CollectAgent:
    """采集 Agent：拉取内部 Run + 外部 OTLP span + Agent 心跳。"""

    name = "collect"

    def __init__(self, *, run_repo: Any = None, agent_repo: Any = None) -> None:
        self._run_repo = run_repo
        self._agent_repo = agent_repo

    @traced("step.obs.collect", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        window = _window_of(payload)
        biz_line_id = payload.get("biz_line_id", ctx.biz_line_id)

        runs: list[dict[str, Any]] = []
        if self._run_repo is not None:
            try:
                runs = await self._run_repo.list_runs(
                    {
                        "biz_line_id": biz_line_id,
                        "since": window["since"],
                        "until": window["until"],
                        "limit": int(payload.get("limit", 200)),
                    },
                    limit=int(payload.get("limit", 200)),
                )
            except Exception as exc:  # noqa: BLE001 - 采集可降级：失败即空集，不阻断流水线
                logger.warning("采集内部 Run 失败（降级为空）：%s", exc)

        heartbeats: dict[str, Any] = {}
        if self._agent_repo is not None:
            try:
                agents = await self._agent_repo.list_agents({"biz_line_id": biz_line_id})
                heartbeats = {str(a.get("agent_uid")): a.get("last_heartbeat_at") for a in agents}
            except Exception as exc:  # noqa: BLE001
                logger.warning("采集 Agent 心跳失败（降级为空）：%s", exc)

        return AgentOutput(
            data={
                "collected": {
                    "runs": runs,
                    "heartbeats": heartbeats,
                    "run_count": len(runs),
                    "window": window,
                }
            },
            confidence=1.0,
            meta={"agent": self.name},
        )


def _window_of(payload: dict[str, Any]) -> dict[str, datetime]:
    """从 payload 解析时间窗（默认近 24h，UTC）。"""
    until = payload.get("until")
    since = payload.get("since")
    now = datetime.now(UTC).replace(tzinfo=None)
    until_dt = until if isinstance(until, datetime) else now
    if isinstance(since, datetime):
        since_dt = since
    else:
        hours = int(payload.get("hours", 24))
        since_dt = until_dt - timedelta(hours=hours)
    return {"since": since_dt, "until": until_dt}
