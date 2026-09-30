"""``/api/v1`` 路由装配。

B0 只提供装配骨架：各端点模块（auth / chatbi / dashboards / semantics /
observability / finops / stream）在 B4~B6 实现。装配时**按存在性动态 include**，
因此 B0 阶段可以零端点启动、``/openapi.json`` 正常生成，而后续批次只是新增文件，
本模块无需改动。
"""

from __future__ import annotations

import importlib
import logging

from fastapi import APIRouter, FastAPI

logger = logging.getLogger("dba.api.v1")

#: （模块路径, 路由前缀, 标签）—— 顺序即 OpenAPI 中的呈现顺序
ROUTER_MODULES: tuple[tuple[str, str, str], ...] = (
    ("dba.api.v1.auth", "/auth", "auth"),
    ("dba.api.v1.chatbi", "/chat", "chatbi"),
    ("dba.api.v1.dashboards", "/dashboards", "dashboards"),
    ("dba.api.v1.semantics", "/semantics", "semantics"),
    ("dba.api.v1.observability", "/obs", "observability"),
    ("dba.api.v1.finops", "/finops", "finops"),
    ("dba.api.v1.stream", "/stream", "stream"),
)


def register_v1(app: FastAPI) -> list[str]:
    """把「已存在」的 v1 路由模块挂到 ``/api/v1`` 下，返回已装配的模块名。

    未实现的模块（B0 阶段全部）会被静默跳过并记 debug 日志。
    """
    registered: list[str] = []
    for module_path, prefix, tag in ROUTER_MODULES:
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            logger.debug("路由模块尚未实现，跳过：%s", module_path)
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router, prefix=f"/api/v1{prefix}", tags=[tag])
            registered.append(module_path)
            logger.info("已装配路由：%s%s", "/api/v1", prefix)
    return registered
