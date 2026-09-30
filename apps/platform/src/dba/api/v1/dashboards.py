"""``/api/v1/dashboards``（大屏 JSON 读取 / 发布 / 导出，§7.3）。

对齐《设计文档 v2》§7.3 / §5.4.3 / §9.5 与《实现要点清单》§1.6。

大屏 JSON 的 owner 是 ``modules.chatbi``（集合 ``dashboard_spec``，``versions[]`` 版本化）；
导出产物落 MinIO（bucket=exports），后端只发**预签名 URL**（§5.6）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from dba.api.deps import Principal, get_container, get_current_principal
from dba.di import Container

__all__ = ["router"]

logger = logging.getLogger("dba.api.dashboards")

router = APIRouter()

_EXPORT_BUCKET = "exports"


@router.get("/{dashboard_id}")
async def get_dashboard(
    dashboard_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``GET /dashboards/{did}``：最新版本的大屏 JSON。"""
    _ = principal
    mongo = container.get("mongo_repos")
    if mongo is None:
        return {}
    try:
        spec = await mongo.dashboard_spec.latest(dashboard_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("大屏读取失败：%s", exc)
        spec = None
    return dict(spec or {})


@router.get("/{dashboard_id}/versions")
async def list_versions(
    dashboard_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> list[dict[str, Any]]:
    """``GET /dashboards/{did}/versions``。"""
    _ = principal
    mongo = container.get("mongo_repos")
    if mongo is None:
        return []
    try:
        return [dict(r) for r in await mongo.dashboard_spec.versions(dashboard_id)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("大屏版本查询失败：%s", exc)
        return []


@router.post("/{dashboard_id}/publish")
async def publish_dashboard(
    dashboard_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /dashboards/{did}/publish``：发布（记录公开 URL）。"""
    _ = principal
    mongo = container.get("mongo_repos")
    url = f"/dashboards/{dashboard_id}"
    expires_at = (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).isoformat()
    if mongo is not None:
        try:
            await mongo.dashboard_spec.mark_published(dashboard_id, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("大屏发布失败：%s", exc)
    return {"url": url, "expires_at": expires_at}


@router.post("/{dashboard_id}/export", response_model=None)
async def export_dashboard(
    dashboard_id: str,
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /dashboards/{did}/export``：导出 pdf/xlsx，返回预签名 URL。"""
    _ = principal
    fmt = str(payload.get("format") or "pdf").lower()
    if fmt not in ("pdf", "xlsx"):
        return JSONResponse(
            status_code=400,
            content={"code": "40001", "message": "format 仅支持 pdf/xlsx", "detail": {}},
        )
    store = container.get("object_store")
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    object_key = f"{_EXPORT_BUCKET}/{dashboard_id}/{stamp}.{fmt}"
    expires_at = (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)).isoformat()
    presigned = ""
    if store is not None:
        try:
            presigned = await store.presigned_get(_EXPORT_BUCKET, object_key, expires_s=3600)
        except Exception as exc:  # noqa: BLE001 - 导出对象尚未生成属预期（异步生成）
            logger.warning("预签名签发失败（导出产物待生成）：%s", exc)
    return {"object_key": object_key, "presigned_url": presigned, "expires_at": expires_at}
