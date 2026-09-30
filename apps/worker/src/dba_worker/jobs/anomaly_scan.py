"""worker 任务：异常扫描（无阈值 EWMA+MAD + 静默失败）。

对齐《设计方案 v2》§6.4、§5.4.6 与《实现要点清单》§3.19、U14、§12.5（可验证产出②）。

★ 无阈值（DoD#2）：真实检测逻辑在模块 03 的 ``AnomalyScanAgent``（owner=observability），
  worker 只负责「按业务线/Agent 维度周期性触发」。检测器**不读任何阈值配置**——触发条件
  完全来自稳健偏差（``|x-μ| > k·MAD``），故「未配置阈值也能产出告警」。
★ P1-4：检测器内部 ``exclude_modules=("observability","finops")``，扫描不会把自己算进业务。
"""

from __future__ import annotations

import logging
from typing import Any

from dba_runtime import ctx as current_ctx

from ._common import active_biz_lines

__all__ = ["run_anomaly_scan"]

logger = logging.getLogger("dba.worker.jobs.anomaly_scan")


async def run_anomaly_scan(
    container: Any,
    *,
    hours: int = 24,
    per_biz_line: bool = True,
    agent_uid: str | None = None,
) -> dict[str, Any]:
    """触发一次异常扫描。

    :param hours: 检测时间窗（近 N 小时）。
    :param per_biz_line: ``True`` 时按启用业务线逐个扫描；``False`` 仅做一次全局扫描。
    :param agent_uid: 指定 Agent 维度（优先于业务线维度）。
    """
    bundle = container.get("observability")
    registry = getattr(bundle, "registry", None) if bundle is not None else None
    if registry is None or not registry.has("anomaly"):
        logger.warning("异常扫描跳过：observability.anomaly 未装配")
        return {"ok": False, "reason": "anomaly_agent_not_assembled"}

    agent = registry.get("anomaly")
    run_ctx = current_ctx()  # 由 ``with_run_context`` 建立的干净上下文

    scopes: list[dict[str, Any]] = []
    if agent_uid:
        scopes.append({"agent_uid": agent_uid})
    elif per_biz_line:
        for line in await active_biz_lines(container):
            # 哨兵 0 表示全局扫描（payload 不带 biz_line_id）
            scopes.append({} if line == 0 else {"biz_line_id": line})
    else:
        scopes.append({})

    total_alerts = 0
    results: list[dict[str, Any]] = []
    for extra in scopes:
        payload: dict[str, Any] = {"hours": hours, **extra}
        output = await agent.run(payload, run_ctx)
        data = dict(output.data or {})
        count = int(data.get("alert_count") or 0)
        total_alerts += count
        results.append({"scope": extra or {"scope": "GLOBAL"}, "alert_count": count})

    logger.info("异常扫描完成 scopes=%d alerts=%d", len(scopes), total_alerts)
    return {"ok": True, "alert_count": total_alerts, "results": results, "hours": hours}
