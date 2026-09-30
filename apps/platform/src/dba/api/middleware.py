"""L1 中间件：TraceMiddleware / RateLimitMiddleware / AuditMiddleware。

对齐《设计文档 v2》§2.1 / §9.3 与《实现要点清单》§1.2。

★ ``TraceMiddleware`` 是 ``run.started`` 事件的**唯一生产者**（§5.10 生产者表）：
  生成 32 位 hex ``trace_id``、建 ``RunContext``、``set_current``、写 ``run:progress``、
  回写响应头 ``X-Trace-Id``。

★ 降级：B0 的 ``run:progress`` 写入 / 限流 / 审计均为**可插拔占位实现**（内存版本），
  由 DI 容器注入；B2 替换为 Redis / ES / MySQL 实现，中间件本身零改动。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from dba_runtime import RunContext, new_span_id, new_trace_id, reset_current, set_current
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from dba.di import Container

logger = logging.getLogger("dba.api.middleware")

# 探针 / 文档路径：**不是业务 Run**，既不限流、不审计，也**不产 run.started 事件**。
# ★ §7.6：``/health`` 是存活探针，不检查依赖；探针路径绝不能因依赖（Redis/ES）故障而 500。
_SKIP_PATHS = frozenset({"/health", "/ready", "/metrics", "/openapi.json", "/docs", "/redoc"})


def _module_for_path(path: str) -> str:
    """按路径前缀推断模块（用于 RunContext.module 与自监控归属）。"""
    if path.startswith("/api/v1/finops"):
        return "finops"
    if path.startswith("/api/v1/obs") or path.startswith("/api/v1/observability"):
        return "observability"
    if path.startswith("/api/v1/chat") or path.startswith("/api/v1/dashboards"):
        return "chatbi"
    return "system"


class TraceMiddleware(BaseHTTPMiddleware):
    """请求追踪：为每个请求建立 RunContext 并绑定 ContextVar。

    ★ P1-9 fail-open + §7.6：``run.started`` 的写入**必须**满足两点，否则 Redis 一挂
    就会把 ``/health`` / ``/ready`` / ``/metrics`` 一起打成 500（K8s liveness 探针失败 →
    反复杀 Pod）。v1 的写法把 ``progress.write`` 放在 skip 判定之前且**无 try/except**，
    正是这个故障：

    1. **探针路径不产 ``run.started``**：按 §9.3/§5.10 生产者映射，``run.started`` 的语义是
       「一次业务 Run 开始」——健康检查不是 Run，不该产生该事件（也避免污染自监控）；
    2. **对 ``progress.write`` 包 try/except**：观测写入失败只记日志并继续（fail-open），
       与 P1-9 的 ``fail_open`` 策略一致。业务请求与探针的成败**不得**取决于观测链路。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = new_trace_id()
        module = _module_for_path(request.url.path)
        ctx = RunContext(trace_id=trace_id, span_id=new_span_id(), module=module)  # type: ignore[arg-type]

        container: Container | None = getattr(request.app.state, "container", None)
        request.state.trace_id = trace_id

        token = set_current(ctx)
        try:
            # ★ 探针路径不是业务 Run：跳过 run.started（限流/审计同理，见 _SKIP_PATHS）
            if container is not None and request.url.path not in _SKIP_PATHS:
                await self._emit_run_started(container, trace_id, module)
            response = await call_next(request)
        finally:
            reset_current(token)

        response.headers["X-Trace-Id"] = trace_id
        return response

    @staticmethod
    async def _emit_run_started(container: Container, trace_id: str, module: str) -> None:
        """写 ``run:progress`` 的 ``run.started``（★ fail-open：失败不阻断请求）。"""
        progress = container.get("progress")
        if progress is None:
            return
        try:
            await progress.write(
                trace_id,
                {
                    "event": "run.started",
                    "seq": 1,
                    "trace_id": trace_id,
                    "ts": int(time.time() * 1000),
                    "data": {"module": module},
                },
            )
        except Exception as exc:  # noqa: BLE001 - P1-9 fail-open：观测写入不得影响业务/探针
            logger.warning("run.started 写入失败（fail-open，已忽略）：%s", exc)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """限流（B0：进程内令牌桶；B2：Redis 令牌桶 + Lua 原子）。"""

    def __init__(self, app: ASGIApp, *, limit: int = 100, burst: int = 200) -> None:
        super().__init__(app)
        self._limit = limit
        self._burst = burst

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        container: Container | None = getattr(request.app.state, "container", None)
        limiter = container.get("rate_limiter") if container is not None else None
        if limiter is not None:
            client = request.client.host if request.client else "unknown"
            allowed = await limiter.allow(f"rl:{client}")
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": "42900",
                        "message": "请求过于频繁",
                        "detail": {},
                        "trace_id": getattr(request.state, "trace_id", None),
                    },
                )
        return await call_next(request)


class AuditMiddleware(BaseHTTPMiddleware):
    """审计：记录请求方法 / 路径 / 状态码 / 耗时（B0 内存；B2 落 ES / MySQL）。

    ★ 审计写入**必须 fail-open**：审计是旁路观测，其失败（ES/Redis 不可用）绝不能把
    业务请求打成 500。这里对 ``sink.write`` 包 try/except，失败只记日志并继续。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        container: Container | None = getattr(request.app.state, "container", None)
        sink = container.get("audit_sink") if container is not None else None
        if sink is not None:
            record: dict[str, Any] = {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": latency_ms,
                "trace_id": getattr(request.state, "trace_id", None),
                "client": request.client.host if request.client else None,
            }
            try:
                await sink.write(record)
            except Exception as exc:  # noqa: BLE001 - 审计旁路失败不得影响业务响应
                logger.warning("审计写入失败（fail-open，已忽略）：%s", exc)
        return response
