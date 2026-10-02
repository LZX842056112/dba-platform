"""``/api/v1/finops``（模块 10 · FinOps，§7.5）。

对齐《设计文档 v2》§7.5 / §6.5 / §5.9 与《实现要点清单》§1.6、U26。

★ 成本时序一律经 ``FinopsService`` → ``ObservabilityService.timeseries``（owner=observability），
  **不直连 ``metric_daily``**（§5.9）。
★ ``POST /finops/guardrail/kill-switch``：全局逃生开关（U26 最小实现，见服务层标注）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from dba.api.deps import Principal, get_container, get_current_principal, require_roles
from dba.api.v1._common import parse_ts, parse_window, service_or_default, storage_or_default
from dba.di import Container

__all__ = ["router"]

logger = logging.getLogger("dba.api.finops")

router = APIRouter()

ADMIN_ROLE_ID = 1
SUPER_ADMIN_ROLE_ID = 1  # 演示：super_admin 与 admin 同一角色；生产需独立角色


@router.get("/cost/summary")
async def cost_summary(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /finops/cost/summary``。"""
    since, until = parse_window(from_, to, span=timedelta(days=30))
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"total": 0, "by_module": {}, "by_biz_line": {}, "by_model": {}}
    result: dict[str, Any] = await service.cost_summary(
        since=since, until=until, biz_line_id=biz_line_id
    )
    return result


@router.get("/cost/timeseries")
async def cost_timeseries(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    group_by: Annotated[str, Query()] = "biz_line",
    granularity: Annotated[str, Query()] = "day",
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /finops/cost/timeseries``。"""
    since, until = parse_window(from_, to, span=timedelta(days=30))
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"series": []}
    result: dict[str, Any] = await service.timeseries(
        group_by=group_by,
        granularity=granularity,
        since=since,
        until=until,
        biz_line_id=biz_line_id,
    )
    return result


@router.get("/cost/attribution")
async def cost_attribution(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    trace_id: Annotated[str, Query(min_length=1)],
) -> dict[str, Any]:
    """``GET /finops/cost/attribution``。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"trace_id": trace_id, "breakdown": []}
    result: dict[str, Any] = await service.attribution(trace_id)
    return result


@router.get("/cost/top-spenders")
async def top_spenders(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    dimension: Annotated[str, Query()] = "model",
    period: Annotated[str, Query()] = "MONTH",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[dict[str, Any]]:
    """``GET /finops/cost/top-spenders``。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return []
    result: list[dict[str, Any]] = await service.top_spenders(
        dimension=dimension, period=period, limit=limit
    )
    return result


@router.get("/cache/stats")
async def cache_stats(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /finops/cache/stats``。"""
    since, until = parse_window(from_, to, span=timedelta(days=30))
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"hit_rate": 0.0, "saved_micro_usd": 0, "by_task": []}
    result: dict[str, Any] = await service.cache_stats(
        since=since, until=until, biz_line_id=biz_line_id
    )
    return result


@router.get("/cost/coverage")
async def cost_coverage(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /finops/cost/coverage``——计价覆盖率（「成本可信」的第一证据）。"""
    since, until = parse_window(from_, to, span=timedelta(days=30))
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"priced_ratio": 0.0, "unpriced_calls": 0, "by_provider": []}
    result: dict[str, Any] = await service.coverage(
        since=since, until=until, biz_line_id=biz_line_id
    )
    return result


@router.get("/curve/reuse-vs-token")
async def curve_reuse_vs_token(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int, Query()] = 0,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """``GET /finops/curve/reuse-vs-token``（★ 带对照组）。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"biz_line_id": biz_line_id, "days": days, "note": "finops 未装配"}
    result: dict[str, Any] = await service.curve(biz_line_id=biz_line_id, days=days)
    return result


@router.get("/budgets")
async def list_budgets(
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
    scope_type: Annotated[str | None, Query()] = None,
    scope_id: Annotated[str | None, Query()] = None,
    period: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /finops/budgets``（admin）。"""
    repos = storage_or_default(container)
    if repos is None:
        return []
    rows: list[dict[str, Any]] = await repos.budget.list(
        {"scope_type": scope_type, "scope_id": scope_id, "period": period}
    )
    return rows


@router.post("/budgets", response_model=None)
async def upsert_budget(
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /finops/budgets``（admin）。"""
    repos = storage_or_default(container)
    if repos is None:
        return JSONResponse(status_code=503, content={"code": "50301", "message": "存储不可用"})
    try:
        budget_id = int(await repos.budget.upsert(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("预算写入失败：%s", exc)
        return JSONResponse(status_code=500, content={"code": "50000", "message": "预算写入失败"})
    return {"budget_id": budget_id}


@router.patch("/budgets/{budget_id}", response_model=None)
async def patch_budget(
    budget_id: int,
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``PATCH /finops/budgets/{id}``（admin，版本 +1）。"""
    repos = storage_or_default(container)
    if repos is None:
        return JSONResponse(status_code=503, content={"code": "50301", "message": "存储不可用"})
    try:
        existing = await repos.budget.get(budget_id)
        if existing is None:
            return JSONResponse(status_code=404, content={"code": "40400", "message": "预算不存在"})
        # 预算采用追加版本；沿用原作用域，生成新主键，避免用旧 id 插入而造成假成功。
        row = {**existing, **payload}
        row.pop("id", None)
        row.pop("created_at", None)
        row.pop("updated_at", None)
        for key in ("scope_type", "scope_id", "period"):
            row[key] = existing[key]
        row["version"] = int(existing.get("version", 1)) + 1
        await repos.budget.upsert(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预算更新失败：%s", exc)
        return JSONResponse(status_code=500, content={"code": "50000", "message": "预算更新失败"})
    return {"version": int(row["version"])}


@router.get("/budgets/{budget_id}/usage")
async def budget_usage(
    budget_id: int,
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``GET /finops/budgets/{id}/usage``（只读，经 BudgetGuard 口径）。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"consumed": 0, "reserved": 0, "remaining": 0, "breaker_state": "CLOSED"}
    result: dict[str, Any] = await service.budget_usage(budget_id)
    return result


@router.get("/prices")
async def list_prices(
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
    provider: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /finops/prices``（admin）。"""
    repos = storage_or_default(container)
    if repos is None:
        return []
    try:
        return [dict(r) for r in await repos.price_book.list_active(provider, model)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("价格表查询失败：%s", exc)
        return []


@router.post("/prices", response_model=None)
async def upsert_price(
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /finops/prices``（admin）。"""
    repos = storage_or_default(container)
    if repos is None:
        return JSONResponse(status_code=503, content={"code": "50301", "message": "存储不可用"})
    try:
        await repos.price_book.upsert(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("价格表写入失败：%s", exc)
        return JSONResponse(status_code=500, content={"code": "50000", "message": "价格写入失败"})
    # ★ 写库后刷新 in-process 价格缓存，让新价格即时生效（无需重启）
    price_cache = container.get("price_cache")
    if price_cache is not None:
        try:
            await price_cache.refresh(repos.price_book)
        except Exception as exc:  # noqa: BLE001 - 刷新失败不阻断写入
            logger.warning("价格缓存刷新失败：%s", exc)
    return {"price_id": payload.get("id")}


@router.post("/prices/sync")
async def sync_prices(
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /finops/prices/sync``（admin）。

    ★ 无外部价格源（供应商价格 API 未接入），语义改为「从 ``price_book`` 重载
    in-process ``price_cache``」，使新增/修改的价格即时生效，无需重启进程。
    """
    _ = payload
    repos = storage_or_default(container)
    price_cache = container.get("price_cache")
    if repos is None or price_cache is None:
        return {"updated": 0, "skipped": 0, "failed": [], "note": "价格缓存未装配"}
    try:
        count = int(await price_cache.refresh(repos.price_book))
    except Exception as exc:  # noqa: BLE001
        logger.warning("价格缓存重载失败：%s", exc)
        return {"updated": 0, "skipped": 0, "failed": [str(exc)], "note": "重载失败"}
    return {
        "updated": count,
        "skipped": 0,
        "failed": [],
        "note": "已从本地 price_book 重载 in-process 价格缓存（外部价格源未接入）",
    }


@router.post("/cost/recompute")
async def recompute_cost(
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /finops/cost/recompute``（admin）：价格修正后重算历史成本。"""
    bundle = container.get("finops")
    start = parse_ts(payload.get("start"))
    end = parse_ts(payload.get("end"))
    if bundle is None or start is None or end is None:
        return {"job_id": None, "note": "缺少 start/end 或 finops 未装配"}
    report = await bundle.attributor.recompute(start.date(), end.date())
    return {"job_id": f"recompute-{start.date()}-{end.date()}", "report": report.as_dict()}


@router.get("/recommendations")
async def list_recommendations(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    scope_type: Annotated[str, Query()] = "GLOBAL",
    scope_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /finops/recommendations``。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return []
    result: list[dict[str, Any]] = await service.recommendations(
        scope_type=scope_type, scope_id=scope_id, status=status
    )
    return result


@router.post("/recommendations/{reco_id}/apply")
async def apply_recommendation(
    reco_id: str,
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /finops/recommendations/{id}/apply``（admin，默认 dry_run）。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"ok": False}
    result: dict[str, Any] = await service.apply_recommendation(
        reco_id, dry_run=bool(payload.get("dry_run", True))
    )
    return result


@router.post("/recommendations/{reco_id}/reject")
async def reject_recommendation(
    reco_id: str,
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /finops/recommendations/{id}/reject``（admin）。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"ok": False}
    result: dict[str, Any] = await service.reject_recommendation(
        reco_id, str(payload.get("reason") or "")
    )
    return result


@router.get("/guardrail/policy")
async def get_guardrail_policy(
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``GET /finops/guardrail/policy``（admin）。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {}
    result: dict[str, Any] = await service.guardrail_policy()
    return result


@router.patch("/guardrail/policy")
async def patch_guardrail_policy(
    payload: dict[str, Any],
    _super: Annotated[Principal, Depends(require_roles(SUPER_ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``PATCH /finops/guardrail/policy``（super_admin）。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {}
    result: dict[str, Any] = await service.update_policy(payload)
    return result


@router.post("/guardrail/kill-switch")
async def kill_switch(
    payload: dict[str, Any],
    _super: Annotated[Principal, Depends(require_roles(SUPER_ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /finops/guardrail/kill-switch``（super_admin）★ 全局逃生开关。"""
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return {"ok": False}
    result: dict[str, Any] = await service.kill_switch(
        enabled=bool(payload.get("enabled", False)), reason=str(payload.get("reason") or "")
    )
    return result


@router.get("/loop-alerts")
async def loop_alerts(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /finops/loop-alerts``。"""
    since, until = parse_window(from_, to, span=timedelta(days=30))
    service = service_or_default(container, "finops_service", None)
    if service is None:
        return []
    result: list[dict[str, Any]] = await service.loop_alerts(since=since, until=until)
    return result
