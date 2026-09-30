"""worker 任务：三方对账（Redis ↔ MySQL ↔ 明细累加）。

对齐《设计方案 v2》§12.6 与《实现要点清单》§6.7（DoD#4：误差 ≤ 1%）。

为什么需要（照抄 v1 会怎样错）
------------------------------
v1 只信 Redis 快路径的 ``consumed``，从不与 MySQL 权威账本、更不与 ``llm_call``/``tool_call``
明细核过。于是「Redis 因故障 fail-open、部分预留未回填」这类漂移会**永远不可见**，直到某天
预算明明没超却被 BLOCK。本任务把三方摆在一起算**相对误差**，超容差即落 ``alert_event``。

三方口径
--------
1. **Redis**：``budget:usage:{id}:{period}`` 的 ``consumed``（快路径，可能漂移）；
2. **MySQL**：``budget_usage.consumed_micro_usd``（权威账本）；
3. **明细累加**：``llm_call`` + ``tool_call`` 的 ``cost_micro_usd`` 之和（事实来源）。

★ 金额一律 micro_usd 整数；相对误差以 ``max(1, 基准)`` 为分母，避免基准为 0 时除零放大噪声。
★ 容差默认 1%（DoD#4）；超出仅**告警**，不自动改账（对账修数须人工，避免自动化越权）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from ._common import period_bounds

__all__ = ["run_reconcile"]

logger = logging.getLogger("dba.worker.jobs.reconcile")

#: 对账容差（DoD#4：≤ 1%）——超出即写告警事件
DEFAULT_TOLERANCE = 0.01


def _rel_error(a: int, b: int) -> float:
    """相对误差 ``|a-b| / max(1, b)``（基准取 ``b``）。"""
    return abs(a - b) / max(1, b)


async def _detail_sum(container: Any, start: datetime, end: datetime) -> int:
    """明细累加：``llm_call`` + ``tool_call`` 的成本之和（micro_usd）。"""
    mysql = container.get("mysql")
    if mysql is None:
        return 0
    from dba.storage.mysql import models as m  # noqa: PLC0415 - 惰性导入，避免耦合可选驱动

    engine = mysql.rw_engine
    async with engine.connect() as conn:
        llm: Any = await conn.scalar(
            select(func.coalesce(func.sum(m.LlmCall.cost_micro_usd), 0)).where(
                m.LlmCall.created_at >= start, m.LlmCall.created_at < end
            )
        )
        tool: Any = await conn.scalar(
            select(func.coalesce(func.sum(m.ToolCall.cost_micro_usd), 0)).where(
                m.ToolCall.created_at >= start, m.ToolCall.created_at < end
            )
        )
    return int(llm or 0) + int(tool or 0)


async def _enabled_budgets(container: Any) -> list[dict[str, Any]]:
    """枚举启用中的预算（``budget.enabled = 1``）。"""
    mysql = container.get("mysql")
    if mysql is None:
        return []
    from dba.storage.mysql import models as m  # noqa: PLC0415

    engine = mysql.rw_engine
    async with engine.connect() as conn:
        result = await conn.execute(select(m.Budget.__table__).where(m.Budget.enabled == 1))
        return [dict(row._mapping) for row in result.fetchall()]


async def _write_drift_alert(
    container: Any, *, observed: int, baseline: int, ratio: float, period: str
) -> int | None:
    """漂移超容差时写一条 ``alert_event``（category=cost_spike, metric=reconcile_drift）。

    ★ 诚实说明：``alert_category`` 枚举（§5.2.5）只含 cost_spike/fail_rate_up/skill_rot/
      silent_failure/loop_suspect，无「对账漂移」专用值，故用 cost_spike 并靠 ``metric``
      字段区分（报告「文档缺口」有登记）。
    """
    repos = container.get("repos")
    if repos is None:
        return None
    row: dict[str, Any] = {
        "severity": "warn",
        "category": "cost_spike",
        "scope_type": "GLOBAL",
        "scope_id": None,
        "metric": "reconcile_drift",
        "observed_value": observed,
        "baseline_value": baseline,
        "robust_zscore": ratio,
        "attribution_json": {
            "period": period,
            "summary": f"对账漂移 {ratio:.4f} 超出容差；observed={observed} baseline={baseline}",
        },
        "suggestion_json": {"action": "manual_reconcile", "auto_applicable": False},
        "status": "open",
    }
    try:
        return int(await repos.alert_event.insert(row))
    except Exception as exc:  # noqa: BLE001 - 告警写失败不致任务失败
        logger.warning("写对账告警失败：%s", exc)
        return None


async def run_reconcile(
    container: Any,
    *,
    period: str = "MONTH",
    anchor: str | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """执行一次三方对账。返回三方总额、逐预算误差与整体结论。"""
    repos = container.get("repos")
    if repos is None:
        logger.warning("对账跳过：repos 未装配")
        return {"ok": False, "reason": "storage_unavailable"}

    # 1) 先回收过期预留，避免「别人家的僵尸预留」污染本次对账
    budget_service = container.get("budget")
    if budget_service is not None:
        try:
            await budget_service.expire_stale_reservations()
        except Exception as exc:  # noqa: BLE001
            logger.warning("回收过期预留失败（继续对账）：%s", exc)

    parsed_anchor = _parse_anchor(anchor)
    start, end = period_bounds(period, parsed_anchor)
    period_start_iso = start.date().isoformat()

    detail_total = await _detail_sum(container, start, end)

    redis = container.get("redis")
    redis_cache = None
    if redis is not None:
        from dba.storage.redis.repo import RedisBudgetCache  # noqa: PLC0415

        redis_cache = RedisBudgetCache(redis)

    budgets = await _enabled_budgets(container)
    rows: list[dict[str, Any]] = []
    mysql_total = 0
    redis_total = 0
    redis_successes = 0
    for budget in budgets:
        budget_id = int(budget.get("id") or 0)
        usage = await repos.budget_usage.get(budget_id, start.date())
        mysql_consumed = int((usage or {}).get("consumed_micro_usd", 0) or 0)
        mysql_total += mysql_consumed

        redis_consumed = 0
        if redis_cache is not None:
            try:
                redis_usage: dict[str, int] = await redis_cache.usage(budget_id, period_start_iso)
                redis_consumed = int(redis_usage.get("consumed", 0))
                redis_successes += 1
            except Exception as exc:  # noqa: BLE001 - Redis 挂了不阻断对账（该方剔除并标注）
                logger.warning("读取 Redis 用量失败 budget_id=%s：%s", budget_id, exc)
        redis_total += redis_consumed

        rows.append(
            {
                "budget_id": budget_id,
                "scope_type": budget.get("scope_type"),
                "scope_id": budget.get("scope_id"),
                "mysql_consumed_micro_usd": mysql_consumed,
                "redis_consumed_micro_usd": redis_consumed,
            }
        )

    # ★ Redis 一方仅在「可用且读取完整」时纳入比较：否则把 0 当成真实用量会造出
    #   100% 的假漂移（Redis 一挂就天天误报），并使 DoD#4 的 1% 判据失去意义。
    redis_comparable = redis_cache is not None and redis_successes == len(budgets)
    errors: dict[str, float] = {"mysql_vs_detail": _rel_error(mysql_total, detail_total)}
    if redis_comparable:
        errors["redis_vs_mysql"] = _rel_error(redis_total, mysql_total)
        errors["redis_vs_detail"] = _rel_error(redis_total, detail_total)
    worst = max(errors.values()) if errors else 0.0
    within_tolerance = worst <= tolerance

    alert_id: int | None = None
    if not within_tolerance:
        alert_id = await _write_drift_alert(
            container,
            observed=mysql_total,
            baseline=detail_total,
            ratio=worst,
            period=f"{period}:{period_start_iso}",
        )

    logger.info(
        "三方对账完成 period=%s mysql=%d redis=%d detail=%d worst=%.4f ok=%s",
        period,
        mysql_total,
        redis_total,
        detail_total,
        worst,
        within_tolerance,
    )
    return {
        "ok": True,
        "period": period,
        "period_start": period_start_iso,
        "totals": {
            "redis_micro_usd": redis_total,
            "mysql_micro_usd": mysql_total,
            "detail_micro_usd": detail_total,
        },
        "errors": {k: round(v, 6) for k, v in errors.items()},
        "worst_error": round(worst, 6),
        "tolerance": tolerance,
        "within_tolerance": within_tolerance,
        "redis_comparable": redis_comparable,
        "budgets": rows,
        "alert_id": alert_id,
    }


def _parse_anchor(anchor: str | None) -> datetime | None:
    """把 ``anchor``（ISO 字符串）解析为 UTC naive ``datetime``。"""
    if not anchor:
        return None
    try:
        return datetime.fromisoformat(anchor).replace(tzinfo=None)
    except ValueError:
        return None
