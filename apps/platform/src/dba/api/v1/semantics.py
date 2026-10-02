"""``/api/v1/semantics``（语义层：口径 / 字段映射 / 检索，§7.3）。

对齐《设计文档 v2》§7.3 / §5.4.8 与《实现要点清单》§1.6。

语义层的 owner 是 L4 ``capabilities.semantics``（``sem_metric`` / ``sem_field_mapping`` /
``sem_dict_entry``）；L1 装配层经 ``repos`` 触达。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from dba.api.deps import Principal, get_container, get_current_principal, require_roles
from dba.di import Container

__all__ = ["router"]

logger = logging.getLogger("dba.api.semantics")

router = APIRouter()

ADMIN_ROLE_ID = 1

_METRIC_FIELDS = {
    "name": "metric_name",
    "metric_name": "metric_name",
    "description": "caliber_desc",
    "caliber_desc": "caliber_desc",
    "expression": "sql_expr",
    "sql_expr": "sql_expr",
    "unit": "unit",
    "include_tax": "include_tax",
    "region_scope": "region_scope",
    "default_dims": "default_dims",
}


@router.get("/metrics")
async def list_metrics(
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    biz_line_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /semantics/metrics``。"""
    repos = container.get("repos")
    if repos is None:
        return []
    bl = biz_line_id if biz_line_id is not None else principal.biz_line_id
    try:
        if q:
            return [dict(r) for r in await repos.sem_metric.search(q, bl, limit=50)]
        # 无关键词时以空串检索（命中全部已登记口径的前 N 条）
        return [dict(r) for r in await repos.sem_metric.search("", bl, limit=50)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("口径列表查询失败：%s", exc)
        return []


@router.get("/metrics/{code}")
async def get_metric(
    code: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``GET /semantics/metrics/{code}``（含版本历史）。"""
    repos = container.get("repos")
    if repos is None:
        return {}
    try:
        rows = await repos.sem_metric.by_codes([code], principal.biz_line_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("口径详情查询失败：%s", exc)
        rows = []
    return dict(rows[0]) if rows else {}


@router.post("/metrics", response_model=None)
async def create_metric(
    payload: dict[str, Any],
    admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /semantics/metrics``（admin）。"""
    repos = container.get("repos")
    if repos is None:
        return JSONResponse(status_code=503, content={"code": "50301", "message": "存储不可用"})
    code = str(payload.get("code") or payload.get("metric_code") or "").strip()
    fields = {
        target: payload[source]
        for source, target in _METRIC_FIELDS.items()
        if source in payload
    }
    required = {"metric_name", "caliber_desc", "sql_expr"}
    if not code or not required.issubset(fields):
        missing = sorted(required - fields.keys())
        return JSONResponse(
            status_code=400,
            content={
                "code": "40001",
                "message": "缺少指标必填字段",
                "detail": {"missing": (["code"] if not code else []) + missing},
                "trace_id": None,
            },
        )
    row = {
        "metric_code": code,
        "biz_line_id": payload.get("biz_line_id"),
        "created_by": admin.user_id,
        "status": 1,
        **fields,
    }
    row.setdefault("unit", "CNY")
    await repos.sem_metric.insert(row)
    return {"metric_id": code, "created": True}


@router.patch("/metrics/{code}", response_model=None)
async def patch_metric(
    code: str,
    payload: dict[str, Any],
    admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``PATCH /semantics/metrics/{code}``（admin，版本 +1）。"""
    repos = container.get("repos")
    if repos is None:
        return JSONResponse(status_code=503, content={"code": "50301", "message": "存储不可用"})
    fields = {
        target: payload[source]
        for source, target in _METRIC_FIELDS.items()
        if source in payload
    }
    if not fields:
        return JSONResponse(status_code=400, content={"code": "40001", "message": "缺少更新字段"})
    row = await repos.sem_metric.patch(code, admin.biz_line_id, fields)
    if row is None:
        return JSONResponse(status_code=404, content={"code": "40400", "message": "指标不存在"})
    return {"version": int(row["version"])}


@router.get("/search")
async def search_semantics(
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    q: Annotated[str, Query(min_length=1)],
    top_k: Annotated[int, Query(ge=1, le=50)] = 8,
) -> list[dict[str, Any]]:
    """``GET /semantics/search``：口径检索（top_k）。"""
    repos = container.get("repos")
    if repos is None:
        return []
    try:
        found: list[dict[str, Any]] = [
            dict(r) for r in await repos.sem_metric.search(q, principal.biz_line_id, limit=top_k)
        ]
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("口径检索失败：%s", exc)
        return []


@router.get("/mappings")
async def list_mappings(
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    logical_field: Annotated[str | None, Query()] = None,
    table: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /semantics/mappings``：逻辑字段 → 物理列映射。"""
    _ = table
    repos = container.get("repos")
    if repos is None:
        return []
    try:
        if logical_field:
            return [dict(r) for r in await repos.sem_field_mapping.by_logical(logical_field)]
        rows = await repos.sem_field_mapping.all_mappings()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("字段映射查询失败：%s", exc)
        return []
