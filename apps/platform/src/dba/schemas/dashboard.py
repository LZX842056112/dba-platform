"""大屏 JSON 的权威 schema（§5.4.3 / §9.5）。

对齐《设计方案 v2》§5.4.3（Mongo ``dashboard_spec``）与 §9.5（前端布局规范）。

★ 三条硬约定（§9.5）
--------------------
1) ``schema_version`` 必填：前端按版本做兼容分支；
2) ``mode='s3'`` 时不内联大结果，前端走 MinIO 预签名 URL 直拉；
3) ``query.scope_hash`` 仅供前端做一致性提示，**不是安全防线**。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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
    "SCHEMA_VERSION",
]

#: 当前大屏 JSON 版本（前端据此做兼容分支）
SCHEMA_VERSION = "1.0"

PanelKind = Literal["chart", "metric_card", "table", "text"]
ChartType = Literal["line", "bar", "pie", "scatter", "map", "gauge"]
DataSourceMode = Literal["inline", "s3"]
Theme = Literal["light", "dark"]


class LayoutItem(BaseModel):
    """栅格中的一个面板占位。"""

    i: str
    x: int
    y: int
    w: int
    h: int
    minW: int | None = None  # noqa: N815 - 与前端 react-grid-layout 字段一致
    minH: int | None = None  # noqa: N815


class Layout(BaseModel):
    """大屏栅格布局（固定 12 列）。"""

    type: Literal["grid"] = "grid"
    cols: int = 12
    rowHeight: int = 40  # noqa: N815 - 与前端字段一致
    gap: list[int] = Field(default_factory=lambda: [8, 8])
    items: list[LayoutItem] = Field(default_factory=list)


class FieldRef(BaseModel):
    """字段引用（编码通道用）。"""

    field: str
    type: str | None = None
    agg: str | None = None


class Encoding(BaseModel):
    """面板编码（x / y / 系列 / 颜色）。"""

    x: FieldRef | None = None
    y: list[FieldRef] = Field(default_factory=list)
    series: FieldRef | None = None
    color: FieldRef | None = None


class ChartSpec(BaseModel):
    """图表类型。"""

    type: ChartType = "line"


class PanelQuery(BaseModel):
    """面板的取数溯源（含 ``scope_hash``，仅前端一致性提示用）。"""

    metric_codes: list[str] = Field(default_factory=list)
    biz_line_id: int | None = None
    scope_hash: str | None = None


class Panel(BaseModel):
    """一个大屏面板。"""

    panel_id: str
    kind: PanelKind
    title: str
    subtitle: str | None = None
    chart: ChartSpec | None = None
    encoding: Encoding | None = None
    dataset: dict[str, Any] = Field(default_factory=dict)  # {"ref": "q1"}
    query: PanelQuery | None = None
    interaction: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)


class DataColumn(BaseModel):
    """内联数据列定义。"""

    name: str
    type: str = "string"


class DataSource(BaseModel):
    """数据源：小结果内联（``inline``）；大结果走 MinIO（``s3``）。"""

    ref: str
    mode: DataSourceMode = "inline"
    columns: list[DataColumn] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    # s3 模式字段
    object_key: str | None = None
    presigned_url: str | None = None
    expires_at: str | None = None
    row_count: int | None = None
    format: str | None = None


class SpecMeta(BaseModel):
    """大屏生成元信息。"""

    generated_by_trace: str
    generated_at: str
    generator: str = "chatbi.visual.v1"


class DashboardSpec(BaseModel):
    """大屏 JSON 权威定义（§5.4.3 / §9.5）。"""

    schema_version: str = SCHEMA_VERSION
    dashboard_id: str
    version: int = 1
    title: str
    theme: Theme = "light"
    layout: Layout
    panels: list[Panel] = Field(default_factory=list)
    data_sources: list[DataSource] = Field(default_factory=list)
    meta: SpecMeta
    published: bool = False
    published_url: str | None = None
    session_id: str | None = None
