"""``/api/v1/obs``（模块 03 · Observability，§7.4）。

对齐《设计文档 v2》§7.4 / §6.4 / §5.9 与《实现要点清单》§1.6、P1-4。

★ 全部只读接口经 ``ObservabilityService``（owner 读取接口）——**不直连 L5 表**。
★ ``GET /obs/self-cost``：平台自身（03/10）消耗，**不进业务成本曲线**（P1-4）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from dba.api.deps import Principal, get_container, get_current_principal
from dba.api.v1._common import parse_window, read_run_doc, service_or_default, storage_or_default
from dba.di import Container

__all__ = ["router"]

logger = logging.getLogger("dba.api.observability")

router = APIRouter()

_AGENT_TOKEN_HEADER = APIKeyHeader(name="X-Agent-Token", scheme_name="AgentToken", auto_error=False)


def _require_agent_token(
    x_agent_token: Annotated[str | None, Security(_AGENT_TOKEN_HEADER)] = None,
) -> str:
    """Agent 上报类接口鉴权（``X-Agent-Token``）。"""
    if not x_agent_token:
        from dba.api.deps import AuthError  # noqa: PLC0415

        raise AuthError("缺少 X-Agent-Token")
    return x_agent_token


@router.get("/overview")
async def overview(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /obs/overview``：四张 KPI 卡 + 趋势 + top agents。"""
    since, until = parse_window(from_, to, span=timedelta(hours=24))
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {"kpi_cards": {}, "trend": {"series": []}, "top_agents": []}
    return dict(await service.overview(since=since, until=until, biz_line_id=biz_line_id))


@router.get("/topology")
async def topology(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /obs/topology``。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {"nodes": [], "edges": []}
    graph: dict[str, Any] = await service.topology(biz_line_id)
    return graph


@router.get("/agents")
async def list_agents(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /obs/agents``。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return []
    rows: list[dict[str, Any]] = await service.agents(biz_line_id=biz_line_id, status=status)
    return rows


@router.get("/agents/{agent_uid}")
async def get_agent(
    agent_uid: str,
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    """``GET /obs/agents/{agent_uid}``。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {}
    result: dict[str, Any] = await service.agent_detail(agent_uid)
    return result


@router.post("/agents/register", response_model=None)
async def register_agent(
    payload: dict[str, Any],
    container: Annotated[Container, Depends(get_container)],
    _token: Annotated[str, Depends(_require_agent_token)],
) -> dict[str, Any] | JSONResponse:
    """``POST /obs/agents/register``（AgentToken）。"""
    repos = storage_or_default(container)
    if repos is None:
        return JSONResponse(status_code=503, content={"code": "50301", "message": "存储不可用"})
    row = {
        "agent_uid": str(payload.get("agent_uid") or ""),
        "name": str(payload.get("name") or ""),
        "biz_line_id": payload.get("biz_line_id"),
        "runtime_type": str(payload.get("runtime_type") or "external_sdk"),
        "version": payload.get("version"),
        "status": "active",
    }
    if not row["agent_uid"]:
        return JSONResponse(status_code=400, content={"code": "40001", "message": "缺少 agent_uid"})
    agent_name = str(payload.get("name") or "").strip()
    allowed_runtime_types = {"internal_pipeline", "external_sdk", "otel_agent"}
    if row["runtime_type"] not in allowed_runtime_types:
        return JSONResponse(
            status_code=400,
            content={
                "code": "40001",
                "message": "runtime_type 无效",
                "detail": {"allowed": sorted(allowed_runtime_types)},
                "trace_id": None,
            },
        )
    if not agent_name:
        return JSONResponse(status_code=400, content={"code": "40001", "message": "缺少 name"})
    row["name"] = agent_name
    try:
        await repos.app_agent.upsert(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agent 注册失败：%s", exc)
        raise
    return {"ok": True}


@router.post("/agents/{agent_uid}/heartbeat")
async def agent_heartbeat(
    agent_uid: str,
    payload: dict[str, Any],
    container: Annotated[Container, Depends(get_container)],
    _token: Annotated[str, Depends(_require_agent_token)],
) -> dict[str, Any]:
    """``POST /obs/agents/{agent_uid}/heartbeat``（AgentToken）。"""
    _ = payload
    repos = storage_or_default(container)
    if repos is None:
        return {"ok": False, "reason": "storage_unavailable"}
    try:
        await repos.app_agent.heartbeat(agent_uid, datetime.now(UTC).replace(tzinfo=None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("心跳写入失败：%s", exc)
        raise
    return {"ok": True}


@router.get("/runs")
async def list_runs(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
    agent_uid: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    module: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /obs/runs``。"""
    _ = agent_uid
    repos = storage_or_default(container)
    if repos is None:
        return []
    since, until = parse_window(from_, to, span=timedelta(hours=24))
    try:
        rows: list[dict[str, Any]] = await repos.run.list_runs(
            {
                "biz_line_id": biz_line_id,
                "status": status,
                "module": module,
                "since": since,
                "until": until,
                "limit": 200,
            },
            limit=200,
        )
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("Run 列表查询失败：%s", exc)
        return []


@router.get("/runs/{trace_id}")
async def get_run(
    trace_id: str,
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    """``GET /obs/runs/{trace_id}``：run_doc（span 树）。"""
    return await read_run_doc(container, trace_id)


@router.get("/self-cost")
async def self_cost(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """★ ``GET /obs/self-cost``：平台自身（03/10）消耗，不进业务成本曲线（P1-4）。"""
    since, until = parse_window(from_, to, span=timedelta(hours=24))
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {"by_module": {}, "total_cost_micro_usd": 0, "total_runs": 0}
    cost: dict[str, Any] = await service.self_cost(since=since, until=until)
    return cost


@router.get("/metrics/timeseries")
async def metrics_timeseries(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    metric: Annotated[str, Query()] = "cost_micro_usd",
    biz_line_id: Annotated[int | None, Query()] = None,
    agent_uid: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /obs/metrics/timeseries``（只读 ``metric_daily``，经 owner 服务）。"""
    since, until = parse_window(from_, to, span=timedelta(hours=24))
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {"series": []}
    series = await service.timeseries(
        metric=metric,
        since=since,
        until=until,
        biz_line_id=biz_line_id,
        agent_uid=agent_uid,
        model=model,
    )
    return {"series": series}


@router.get("/metrics/skills")
async def metrics_skills(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /obs/metrics/skills``：总数 / 复用率 / 死技能。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {"total": 0, "reuse_rate": 0.0, "dead_count": 0, "series": []}
    metrics = await service.skill_metrics(datetime.now(UTC).date(), biz_line_id)
    if metrics is None:
        return {"total": 0, "reuse_rate": 0.0, "dead_count": 0, "series": []}
    return {
        "total": metrics.total,
        "used": metrics.used,
        "reuse_rate": metrics.reuse_rate,
        "dead_count": metrics.dead_count,
        "series": [],
    }


@router.get("/metrics/memory")
async def metrics_memory(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /obs/metrics/memory``：记忆命中率。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {"hit_rate": 0.0, "lookups": 0, "hits": 0, "series": []}
    metrics = await service.memory_metrics(datetime.now(UTC).date(), biz_line_id)
    if metrics is None:
        return {"hit_rate": 0.0, "lookups": 0, "hits": 0, "series": []}
    return {
        "hit_rate": metrics.hit_rate,
        "lookups": metrics.lookups,
        "hits": metrics.hits,
        "series": metrics.series,
    }


@router.get("/skills")
async def obs_skills(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    biz_line_id: Annotated[int | None, Query()] = None,
    is_dead: Annotated[bool | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /obs/skills``（技能 + 统计）。"""
    _ = is_dead
    repos = storage_or_default(container)
    if repos is None:
        return []
    try:
        rows = await repos.skill_registry.list_for_observability(biz_line_id, is_dead)
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("技能列表查询失败：%s", exc)
        return []


@router.get("/anomalies")
async def list_anomalies(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /obs/anomalies``（``alert_event``）。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return []
    rows: list[dict[str, Any]] = await service.anomalies(
        status=status, category=category, severity=severity
    )
    return rows


@router.get("/anomalies/{alert_id}")
async def anomaly_detail(
    alert_id: int,
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    """``GET /obs/anomalies/{id}``：``alert_event`` + ``anomaly_report``（U14）。"""
    service = service_or_default(container, "observability_service", None)
    if service is None:
        return {}
    return await service.anomaly_detail(alert_id) or {}


async def _ack_alert(alert_id: int, container: Container, principal: Principal) -> dict[str, Any]:
    """将告警标记为已确认；数据库状态是唯一结果依据。"""
    repos = storage_or_default(container)
    if repos is None:
        return {"ok": False, "reason": "storage_unavailable"}
    try:
        await repos.alert_event.ack(alert_id, principal.user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("异常确认失败：%s", exc)
        raise
    return {"ok": True}


async def _resolve_alert(alert_id: int, container: Container) -> dict[str, Any]:
    """将 open/acked 告警置为 resolved，重复操作保持幂等。"""
    repos = storage_or_default(container)
    if repos is None:
        return {"ok": False, "reason": "storage_unavailable"}
    try:
        await repos.alert_event.resolve(alert_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("异常解决失败：%s", exc)
        raise
    return {"ok": True}


@router.post("/anomalies/{alert_id}/ack")
async def ack_anomaly(
    alert_id: int,
    payload: dict[str, Any],
    container: Annotated[Container, Depends(get_container)],
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    """``POST /obs/anomalies/{id}/ack``。"""
    _ = payload
    return await _ack_alert(alert_id, container, principal)


@router.post("/anomalies/{alert_id}/resolve")
async def resolve_anomaly(
    alert_id: int,
    payload: dict[str, Any],
    container: Annotated[Container, Depends(get_container)],
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    """``POST /obs/anomalies/{id}/resolve``：将告警置为已解决。"""
    _ = (payload, principal)
    return await _resolve_alert(alert_id, container)


@router.post("/otel/v1/traces", response_model=None)
async def otel_traces(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
    _token: Annotated[str, Depends(_require_agent_token)],
) -> dict[str, Any] | JSONResponse:
    """``POST /obs/otel/v1/traces``：OTLP/HTTP（JSON）接入（§7.4）。"""
    bundle = container.get("observability")
    ingester = getattr(bundle, "otel", None) if bundle is not None else None
    if ingester is None:
        return {"partialSuccess": {"rejectedSpans": 0, "errorMessage": "observability 未装配"}}
    content_type = request.headers.get("content-type", "")
    if "protobuf" in content_type:
        proto_result = await ingester.ingest_protobuf(await request.body())
        proto_out: dict[str, Any] = proto_result.as_dict()
        return proto_out
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非法 JSON
        return JSONResponse(status_code=400, content={"code": "40001", "message": "非法 JSON"})
    result = await ingester.ingest(payload)
    out: dict[str, Any] = result.as_dict()
    return out


@router.get("/costs/attribution")
async def obs_attribution(
    container: Annotated[Container, Depends(get_container)],
    _principal: Annotated[Principal, Depends(get_current_principal)],
    trace_id: Annotated[str, Query(min_length=1)],
) -> dict[str, Any]:
    """``GET /obs/costs/attribution``（四维归因）。"""
    finops = container.get("finops_service")
    if finops is None:
        return {"trace_id": trace_id, "breakdown": []}
    breakdown: dict[str, Any] = await finops.attribution(trace_id)
    return breakdown
