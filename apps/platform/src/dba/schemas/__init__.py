"""Pydantic DTO（API 层与内部共用）。

对齐《实现要点清单》§2.3 / §5.8。

* ``dashboard``：大屏 JSON 的**权威 schema**（§5.4.3 / §9.5），由 ``spec_builder`` 产出、
  前端 ``PanelRenderer`` 完全由它驱动；
* ``dto``：ChatBI 主链路的请求 / 响应 DTO（API 层使用）。

★ 约定：金额一律 micro_usd 整数；时间一律 UTC（§6.1 / §6.2）。
"""

from __future__ import annotations

from .dashboard import (
    ChartSpec,
    DashboardSpec,
    DataColumn,
    DataSource,
    Encoding,
    FieldRef,
    Layout,
    LayoutItem,
    Panel,
    PanelQuery,
    SpecMeta,
)
from .dto import (
    ChatMessageOut,
    QueryAccepted,
    QueryOptions,
    QueryRequest,
    SessionCreateOut,
    SqlAuditOut,
)

__all__ = [
    "LayoutItem",
    "Layout",
    "FieldRef",
    "Encoding",
    "ChartSpec",
    "PanelQuery",
    "Panel",
    "DataColumn",
    "DataSource",
    "SpecMeta",
    "DashboardSpec",
    "QueryOptions",
    "QueryRequest",
    "QueryAccepted",
    "SessionCreateOut",
    "ChatMessageOut",
    "SqlAuditOut",
]
