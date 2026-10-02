"""API v1 共享工具（时间窗解析 + 服务降级取用）。

本模块收敛两类在多个 router 里反复出现的样板代码：

1. **时间窗解析**：finops / observability 都用「``from/to`` + ISO8601 解析 +
   缺省回退到近 N 小时/天」，统一到 ``parse_ts`` / ``parse_window``，
   避免解析规则改一处漏一处。
2. **可选服务降级**：接口层对「模块未装配 / 存储不可用」的统一处理是
   「返回一个空壳默认值，不 500」。此前每个 handler 都手写
   ``service = container.get(...); if service is None: return {...}``，
   30+ 处重复且默认值散落各处；``service_or_default`` 把它收敛成一行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from dba.di import Container

__all__ = ["parse_ts", "parse_window", "service_or_default", "storage_or_default"]


def parse_ts(value: str | None) -> datetime | None:
    """解析 ISO8601 时间串为 naive UTC datetime；空 / 非法返回 ``None``。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def parse_window(
    from_: str | None, to: str | None, *, span: timedelta
) -> tuple[datetime, datetime]:
    """解析 ``from/to`` 时间窗；缺省回退到「近 ``span``」窗口（UTC naive）。

    右端点缺省取「当前时刻」，左端点缺省为 ``右端点 - span``；半开区间 ``[since, until)``。
    """
    until = parse_ts(to) or datetime.now(UTC).replace(tzinfo=None)
    since = parse_ts(from_) or (until - span)
    return since, until


def service_or_default(container: Container, key: str, default: Any) -> Any:
    """取容器中的可选服务；未装配 / 存储不可用时返回 ``default``。

    用途：把「模块降级」的样板逻辑收敛成一行，例如::

        service = service_or_default(container, "finops_service", None)
        if service is None:
            return {"total": 0}

    与直接 ``container.get(key)`` 的区别只在于**语义命名**：本函数用于表达
    「这是可选服务，缺失要降级」，让 `container.get(key, default)` 的用法
    与「必须存在的依赖」区分开，便于检索所有降级点。

    参数：``container`` DI 容器；``key`` 服务键；``default`` 缺省返回值。
    返回：服务实例或 ``default``。
    """
    value = container.get(key)
    return default if value is None else value


def storage_or_default(container: Container, default: Any = None) -> Any:
    """取 MySQL Repository 聚合（``repos``）；存储不可用时返回 ``default``。

    薄封装：``repos`` 是接口层最常用的可选依赖（列表类接口在存储不可用时统一返回 ``[]``）。
    """
    return service_or_default(container, "repos", default)
