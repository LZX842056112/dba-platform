"""应用装配：``create_app()``。

对齐《设计文档 v2》§4.2 / §6.1.4 / §7.6 与《实现要点清单》§1.1、§6.7。

★ lifespan 内必须完成三件事（v1 缺一即成为「最难发现的故障」）：
  ① ``bind_metering(service, cost=...)``：未绑定则埋点是静默 no-op；
  ② 启动 ``drain_queue`` 后台投递协程；
  ③ ``set_current`` 包裹启动上下文（worker job 同理）。
★ ``/ready`` 暴露 ``metering_bound``，并逐项返回依赖组件 + 降级语义（§7.6）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

from dba_runtime import (
    RunContext,
    bind_metering,
    drain_queue,
    dropped_total,
    is_bound,
    new_span_id,
    new_trace_id,
    queue_depth,
    reset_current,
    set_current,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.responses import Response

from .api.errors import register_exception_handlers
from .api.middleware import AuditMiddleware, RateLimitMiddleware, TraceMiddleware
from .api.v1 import register_v1
from .config import HARD_DEPENDENCIES, Settings, get_settings
from .di import Container, build_container, warmup

logger = logging.getLogger("dba.main")


def _make_lifespan(
    container: Container, settings: Settings
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _ = app

        # ⓿ 异步预热：价格缓存 / Redis Lua 脚本（B3）
        await warmup(container)

        metering = container.get("metering")
        cost = container.get("cost_normalizer")

        # ① 必须绑定，否则埋点静默 no-op
        bind_metering(metering, cost=cost)
        if not is_bound():
            raise RuntimeError("bind_metering 失败：埋点未装配，拒绝启动")

        # ③ 用 RunContext 包裹启动上下文
        start_ctx = RunContext(trace_id=new_trace_id(), span_id=new_span_id(), module="system")
        token = set_current(start_ctx)

        # ② 后台任务：内核埋点投递 + outbox 投递器（P1-6）
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(drain_queue(metering), name="telemetry-drain")
        ]
        dispatcher = container.get("dispatcher")
        if dispatcher is not None:
            tasks.append(asyncio.create_task(dispatcher.run_forever(), name="outbox-dispatch"))
        logger.info(
            "dba 平台启动 env=%s deadline=%ss metering_bound=%s storage=%s",
            settings.env,
            settings.run_deadline_s,
            is_bound(),
            container.get("storage_available"),
        )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await container.aclose()
            reset_current(token)
            logger.info("dba 平台已优雅关闭")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用（含中间件 / 异常处理 / 运维端点 / v1 路由）。"""
    settings = settings or get_settings()
    container = build_container(settings)

    app = FastAPI(
        title="数据大屏 × 多 Agent 平台",
        version="0.1.0",
        lifespan=_make_lifespan(container, settings),
    )
    app.state.settings = settings
    app.state.container = container

    register_exception_handlers(app)

    # 中间件装配：后 add 的在外层 → Trace 最外层，保证 trace_id 覆盖全部处理
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TraceMiddleware)

    _register_ops_routes(app, settings)
    register_v1(app)
    return app


def _register_ops_routes(app: FastAPI, settings: Settings) -> None:
    """注册运维端点：/health、/ready、/metrics。"""

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        """存活探针（不检查依赖）。"""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready(request: Request) -> Response:
        """就绪探针：逐项探测依赖组件 + 降级语义 + metering_bound。"""
        container: Container = request.app.state.container
        probe = container.get("health_probe")
        components: dict[str, dict[str, object]] = await probe.probe() if probe is not None else {}

        hard_down = all(not components.get(name, {}).get("ok", False) for name in HARD_DEPENDENCIES)
        any_down = any(not info.get("ok", False) for info in components.values())

        if hard_down:
            status, http_status = "not_ready", 503
        elif any_down:
            status, http_status = "degraded", 200
        else:
            status, http_status = "ok", 200

        body = {
            "status": status,
            "components": components,
            "metering_bound": is_bound(),
            "env": settings.env,
        }
        return JSONResponse(body, status_code=http_status)

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        """Prometheus 自监控指标（内网）。"""
        lines = [
            "# HELP telemetry_dropped_total 埋点入队被丢弃的条数",
            "# TYPE telemetry_dropped_total counter",
            f"telemetry_dropped_total {dropped_total()}",
            "# HELP telemetry_queue_depth 待投递队列深度",
            "# TYPE telemetry_queue_depth gauge",
            f"telemetry_queue_depth {queue_depth()}",
            "# HELP dba_metering_bound 埋点是否已装配",
            "# TYPE dba_metering_bound gauge",
            f"dba_metering_bound {1 if is_bound() else 0}",
            "# HELP dba_build_info 构建信息",
            "# TYPE dba_build_info gauge",
            f'dba_build_info{{env="{settings.env}",version="0.1.0"}} 1',
        ]
        return PlainTextResponse("\n".join(lines) + "\n")


# 供 uvicorn 直接引用：`uvicorn dba.main:app`
app = create_app()
