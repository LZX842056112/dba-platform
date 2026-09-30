"""统一错误体与异常处理器。

对齐《设计文档 v2》§7.1 与《实现要点清单》§1.3、§6.3、§6.9（含 ★ v2 新增 50013/50014/50015）。

统一错误体：``{"code": "40001", "message": "...", "detail": {...}, "trace_id": "..."}``。
本模块把内核的**符号码**（``DbaError.symbol``）映射到**数值码**（§7.1 错误码表）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from dba_runtime import DbaError, ctx_or_none
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("dba.errors")

#: 数值码 → HTTP 状态（§7.1 错误码表）
CODE_HTTP: dict[str, int] = {
    "40001": 400,
    "40100": 401,
    "40300": 403,
    "40400": 404,
    "40900": 409,
    "42900": 429,
    "42901": 429,
    "50000": 500,
    "50010": 422,
    "50011": 422,
    "50012": 422,
    "50013": 408,
    "50014": 422,
    "50015": 403,
    "50301": 503,
}

#: 符号码 → 数值码（覆盖内核全部符号码 + 护栏的 SQL_* 细分）
SYMBOL_TO_CODE: dict[str, str] = {
    "VALIDATION_ERROR": "40001",
    "UNAUTHENTICATED": "40100",
    "NOT_FOUND": "40400",
    "CONFLICT": "40900",
    "RATE_LIMITED": "42900",
    "BUDGET_EXCEEDED": "42901",
    "INTERNAL_ERROR": "50000",
    "TELEMETRY_NOT_BOUND": "50000",
    "STEP_TIMEOUT": "50013",
    "RUN_DEADLINE_EXCEEDED": "50013",
    "STEP_VISIT_EXCEEDED": "50014",
    "SQL_SCOPE_EMPTY": "50015",
    "STORAGE_UNAVAILABLE": "50301",
    "RETRY_EXHAUSTED": "50012",
    # 策略性拒绝（不重试）
    "SQL_PERMISSION_DENIED": "40300",
    "SQL_GUARD_BLOCKED": "50010",
    "SQL_PARSE_ERROR": "50011",
    "SQL_MULTI_STATEMENT": "50010",
    "SQL_NOT_READONLY": "50010",
    "SQL_DANGEROUS_CONSTRUCT": "50010",
    "SQL_DANGEROUS_FUNCTION": "50010",
    "SQL_TABLE_NOT_ALLOWED": "50010",
    "SQL_FUNCTION_NOT_ALLOWED": "50010",
    "SQL_DIALECT_UNSUPPORTED": "50010",
    "SQL_SCOPE_JOIN_MISSING": "50015",
    "SQL_SCOPE_NOT_INJECTED": "50015",
    # 技术性失败（可回退）
    "SQL_EXEC_TRANSIENT": "50011",
    "SQL_DRY_RUN_FAILED": "50011",
}

#: HTTP 状态 → 默认数值码（兜底 StarletteHTTPException / 404 等）
HTTP_TO_CODE: dict[int, str] = {
    400: "40001",
    401: "40100",
    403: "40300",
    404: "40400",
    409: "40900",
    429: "42900",
    500: "50000",
    503: "50301",
}


def numeric_code(symbol_or_code: str) -> str:
    """符号码 / 数值码统一归一为数值码。"""
    if symbol_or_code.isdigit():
        return symbol_or_code
    return SYMBOL_TO_CODE.get(symbol_or_code, "50000")


def build_error_body(
    code: str, message: str, *, detail: dict[str, Any] | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    """构造统一错误体。``trace_id`` 缺省时从当前 RunContext 读取。"""
    if trace_id is None:
        current = ctx_or_none()
        trace_id = current.trace_id if current is not None else None
    return {
        "code": code,
        "message": message,
        "detail": detail or {},
        "trace_id": trace_id,
    }


def _trace_id(request: Request) -> str | None:
    value = getattr(request.state, "trace_id", None)
    if isinstance(value, str):
        return value
    current = ctx_or_none()
    return current.trace_id if current is not None else None


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理器。"""

    @app.exception_handler(DbaError)
    async def _dba_error_handler(request: Request, exc: DbaError) -> JSONResponse:
        code = numeric_code(exc.symbol if exc.symbol else exc.code)
        http_status = CODE_HTTP.get(code, exc.http_status)
        body = build_error_body(code, exc.message, detail=exc.detail, trace_id=_trace_id(request))
        return JSONResponse(status_code=http_status, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=build_error_body(
                "40001",
                "参数校验失败",
                detail={"errors": _jsonable_errors(exc.errors())},
                trace_id=_trace_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTP_TO_CODE.get(exc.status_code, "50000")
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(code, str(exc.detail), trace_id=_trace_id(request)),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content=build_error_body("50000", "内部错误", trace_id=_trace_id(request)),
        )


def _jsonable_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """把 pydantic 校验错误里的非 JSON 类型（如 ValueError 实例）转成字符串。"""
    out: list[dict[str, Any]] = []
    for item in errors:
        clean = dict(item)
        if "ctx" in clean and isinstance(clean["ctx"], dict):
            clean["ctx"] = {k: str(v) for k, v in clean["ctx"].items()}
        out.append(clean)
    return out
