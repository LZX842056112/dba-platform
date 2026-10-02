"""``DashboardSpecBuilder``：把查询结果组装成**大屏 JSON**。

对齐《设计方案 v2》§5.4.3 / §9.5 与《实现要点清单》§5.8（3.16）。

★ 两条路径
----------
* ``from_skill``：命中技能模板 → **确定性路径**（省 token，结果稳定）；
* ``from_llm_json``：LLM 返回 JSON 布局 → 解析并校验。

★ 大结果不内联（§9.5 硬约定 2）
------------------------------
超过内联阈值（默认 500 行）的结果必须走 MinIO 预签名 URL，**不经过应用服务器**——
否则大结果集会占满应用带宽与内存。组装阶段只标注 ``mode='s3'`` + ``object_key``；
实际上传与签发预签名 URL 由 ``materialize()`` 异步完成（VisualAgent 调用）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from dba_runtime.context import RunContext, new_ulid

from dba.schemas.dashboard import (
    ChartSpec,
    DashboardSpec,
    DataColumn,
    DataSource,
    Encoding,
    Layout,
    LayoutItem,
    Panel,
    PanelQuery,
    PanelStyle,
    SpecMeta,
)
from dba.util.jsonx import loads_object, loads_or

__all__ = ["DashboardSpecBuilder"]

logger = logging.getLogger("dba.modules.chatbi.spec_builder")

#: 内联行数阈值（超过则改走 s3）
INLINE_ROW_THRESHOLD = 500
#: 无 `dataset.ref` 时的默认数据源引用
DEFAULT_REF = "q1"
#: 单面板兜底尺寸（12 列栅格）
_PANEL_W = 6
_PANEL_H = 8

#: 驾驶舱默认栅格预设 (x, y, w, h) —— 面板未自带坐标时按序号取用。
#: 版式：一行 4 个 KPI → 大地图 + 右侧两块 → 底部三块。
_COCKPIT_LAYOUT: tuple[tuple[int, int, int, int], ...] = (
    (0, 0, 3, 5),  # KPI 1
    (3, 0, 3, 5),  # KPI 2
    (6, 0, 3, 5),  # KPI 3
    (9, 0, 3, 5),  # KPI 4
    (0, 5, 8, 16),  # 地图（大）
    (8, 5, 4, 8),  # 环形占比
    (8, 13, 4, 8),  # 仪表盘
    (0, 21, 4, 12),  # 排行榜
    (4, 21, 4, 12),  # 堆叠柱
    (8, 21, 4, 12),  # 趋势
)

#: 合法的面板种类与图表类型（用于白名单降级，避免未知值崩掉整个 visual 步骤）
_PANEL_KINDS = frozenset({"chart", "metric_card", "table", "text", "ranking"})
_CHART_TYPES = frozenset({"line", "bar", "pie", "scatter", "map", "gauge"})


class DashboardSpecBuilder:
    """大屏 JSON 组装器（owner: modules.chatbi）。"""

    def __init__(
        self,
        *,
        object_store: Any = None,
        bucket: str = "dba-query-results",
        inline_threshold: int = INLINE_ROW_THRESHOLD,
        presign_expires_s: int = 3600,
    ) -> None:
        self._object_store = object_store
        self._bucket = bucket
        self._threshold = inline_threshold
        self._presign_expires_s = presign_expires_s

    # ── 两条组装路径 ─────────────────────────────────────────────
    def from_skill(
        self, skill_spec: dict[str, Any], payload: dict[str, Any], ctx: RunContext | None = None
    ) -> DashboardSpec:
        """确定性路径：技能模板 → 大屏 JSON。"""
        panels = self._skill_panels(skill_spec)
        if not panels:  # 模板没给面板 → 退化为「按结果字段自动出图」
            panels = self._auto_panels(payload)
        return self._assemble(payload, panels, ctx)

    def from_llm_json(
        self, raw_text: str, payload: dict[str, Any], ctx: RunContext | None = None
    ) -> DashboardSpec:
        """LLM 路径：解析模型返回的 JSON 布局（容错取第一个 JSON 块）。"""
        panels = self._parse_llm_panels(raw_text)
        if not panels:
            panels = self._auto_panels(payload)
        return self._assemble(payload, panels, ctx)

    # ── 结果物化（大结果 → MinIO，异步）───────────────────────────
    async def materialize(self, spec: DashboardSpec, payload: dict[str, Any]) -> DashboardSpec:
        """把 ``mode='s3'`` 的数据源上传到 MinIO 并签发预签名 URL。

        无可用的对象存储时**保持 s3 标注但不签发 URL**并记警告——绝不假装成功，
        也绝不把大结果偷偷内联（那会占满应用内存）。

        ★ 按 ``source.ref`` 取对应查询结果：多查询场景下每个数据源的行集不同，
        此前对所有源都上传 ``payload["rows"]`` 是错的。
        """
        results = self._query_results(payload)
        for source in spec.data_sources:
            if source.mode != "s3" or source.presigned_url:
                continue
            if self._object_store is None:
                logger.warning("需 s3 数据源但对象存储不可用，object_key=%s", source.object_key)
                continue
            rows, columns = results.get(source.ref, ([], []))
            key = source.object_key or f"{spec.dashboard_id}/{source.ref}.json"
            body = json.dumps(
                {"columns": columns, "rows": rows}, ensure_ascii=False, default=str
            ).encode("utf-8")
            source.object_key = key
            try:
                await self._object_store.put(self._bucket, key, body, "application/json")
                source.presigned_url = await self._object_store.presigned_get(
                    self._bucket, key, expires_s=self._presign_expires_s
                )
                source.expires_at = (
                    dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self._presign_expires_s)
                ).isoformat()
            except Exception as exc:  # noqa: BLE001 - 上传失败不阻断出图（前端可刷新重取）
                logger.warning("大结果上传失败：%s", exc)
        return spec

    # ── 内部：组装 ───────────────────────────────────────────────
    def _query_results(
        self, payload: dict[str, Any]
    ) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, str]]]]:
        """归一化「多查询结果」为 ``{ref: (rows, columns)}``。

        有 ``payload["query_results"]``（多查询）则逐 ref 取用；
        否则回退到单查询（``payload["rows"]/["columns"]`` → ``DEFAULT_REF``），
        **保证单查询路径行为完全不变**。
        """
        raw = payload.get("query_results")
        if isinstance(raw, dict) and raw:
            out: dict[str, tuple[list[dict[str, Any]], list[dict[str, str]]]] = {}
            for ref, item in raw.items():
                if not isinstance(item, dict):
                    continue
                rows = list(item.get("rows") or [])
                columns = item.get("columns") or self._columns_from_rows(rows)
                out[str(ref)] = (rows, columns)
            if out:
                return out
        rows = list(payload.get("rows") or [])
        columns = payload.get("columns") or self._columns_from_rows(rows)
        return {DEFAULT_REF: (rows, columns)}

    @staticmethod
    def _layout_for(idx: int, raw: dict[str, Any]) -> tuple[int, int, int, int]:
        """面板栅格位置：``raw`` 显式给的就用，否则按驾驶舱预设兜底。"""
        preset = _COCKPIT_LAYOUT[idx % len(_COCKPIT_LAYOUT)]
        x = int(raw["x"]) if raw.get("x") is not None else preset[0]
        y = int(raw["y"]) if raw.get("y") is not None else preset[1]
        w = int(raw.get("w") or preset[2])
        h = int(raw.get("h") or preset[3])
        return x, y, w, h

    def _assemble(
        self,
        payload: dict[str, Any],
        panel_dicts: list[dict[str, Any]],
        ctx: RunContext | None,
    ) -> DashboardSpec:
        scope_hash = payload.get("scope_hash")
        biz_line_id = payload.get("biz_line_id")
        metric_codes = list(payload.get("metric_codes") or [])
        results = self._query_results(payload)

        panels: list[Panel] = []
        items: list[LayoutItem] = []
        for idx, raw in enumerate(panel_dicts):
            panel_id = str(raw.get("panel_id") or f"p{idx + 1}")
            x, y, w, h = self._layout_for(idx, raw)
            items.append(LayoutItem(i=panel_id, x=x, y=y, w=w, h=h, minW=3, minH=5))
            dataset = dict(raw.get("dataset") or {"ref": DEFAULT_REF})
            # ★ 兜底：真实模型可能编造不存在的 ref → 该面板会恒「暂无数据」。
            #   不在实际结果集里就回退到主查询。
            ref = str(dataset.get("ref") or DEFAULT_REF)
            if ref not in results:
                logger.warning(
                    "面板 %s 的 dataset.ref=%s 不存在，回退 %s", panel_id, ref, DEFAULT_REF
                )
                dataset["ref"] = DEFAULT_REF
            try:
                panels.append(
                    self._panel(panel_id, raw, dataset, metric_codes, biz_line_id, scope_hash)
                )
            except Exception as exc:  # noqa: BLE001 - 单面板配置错误绝不打断整个 visual 步骤
                logger.warning("面板 %s 配置无效，降级为文本面板：%s", panel_id, exc)
                panels.append(
                    Panel(
                        panel_id=panel_id,
                        kind="text",
                        title=str(raw.get("title") or panel_id),
                        text="面板配置无效，已降级展示。",
                    )
                )

        sources = [
            self._data_source(ref, rows, columns, payload)
            for ref, (rows, columns) in results.items()
        ]
        trace_id = ctx.trace_id if ctx is not None else str(payload.get("trace_id") or "")
        return DashboardSpec(
            dashboard_id="dsh_" + new_ulid(),
            version=1,
            title=str(payload.get("title") or payload.get("question") or "数据大屏"),
            layout=Layout(items=items),
            panels=panels,
            data_sources=sources,
            meta=SpecMeta(
                generated_by_trace=trace_id,
                generated_at=dt.datetime.now(dt.UTC).isoformat(),
                generator="chatbi.visual.v2",
            ),
            session_id=payload.get("session_id"),
        )

    def _panel(
        self,
        panel_id: str,
        raw: dict[str, Any],
        dataset: dict[str, Any],
        metric_codes: list[str],
        biz_line_id: int | None,
        scope_hash: str | None,
    ) -> Panel:
        kind = str(raw.get("kind") or "chart")
        if kind not in _PANEL_KINDS:
            logger.warning("未知 panel.kind=%s，降级为 chart", kind)
            kind = "chart"
        chart_obj = raw.get("chart")
        chart_raw: dict[str, Any] = chart_obj if isinstance(chart_obj, dict) else {}
        chart_type = str(chart_raw.get("type") or "line")
        if chart_type not in _CHART_TYPES:
            logger.warning("未知 chart.type=%s，降级为 line", chart_type)
            chart_type = "line"
        encoding_raw = raw.get("encoding")
        return Panel(
            panel_id=panel_id,
            kind=kind,  # type: ignore[arg-type]
            title=str(raw.get("title") or panel_id),
            subtitle=raw.get("subtitle"),
            chart=ChartSpec(type=chart_type, region=chart_raw.get("region")),  # type: ignore[arg-type]
            encoding=Encoding.model_validate(encoding_raw)
            if isinstance(encoding_raw, dict)
            else None,
            dataset=dataset,
            query=PanelQuery(
                metric_codes=list(raw.get("metric_codes") or metric_codes),
                biz_line_id=biz_line_id,
                scope_hash=scope_hash,
            ),
            interaction=dict(raw.get("interaction") or {}),
            style=self._style(raw.get("style")),
            text=raw.get("text"),
        )

    @staticmethod
    def _style(raw: Any) -> PanelStyle:
        """解析 ``panel.style``（失败则退回空样式，不打断组装）。"""
        if not isinstance(raw, dict):
            return PanelStyle()
        try:
            return PanelStyle.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - 样式错误不该让面板消失
            logger.warning("panel.style 非法，已忽略：%s", exc)
            return PanelStyle()

    def _data_source(
        self,
        ref: str,
        rows: list[dict[str, Any]],
        columns: list[dict[str, str]],
        payload: dict[str, Any],
    ) -> DataSource:
        if len(rows) > self._threshold:
            return DataSource(
                ref=ref,
                mode="s3",
                object_key=f"{payload.get('dashboard_key', 'dsh')}/{ref}.json",
                row_count=len(rows),
                format="json",
            )
        matrix = [[row.get(col["name"]) for col in columns] for row in rows]
        return DataSource(
            ref=ref,
            mode="inline",
            columns=[DataColumn(**col) for col in columns],
            rows=matrix,
            row_count=len(rows),
        )

    @staticmethod
    def _columns_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not rows:
            return []
        first = rows[0]
        out: list[dict[str, str]] = []
        for name, value in first.items():
            kind = (
                "number"
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else "string"
            )
            if isinstance(value, str) and _looks_like_date(value):
                kind = "date"
            out.append({"name": str(name), "type": kind})
        return out

    # ── 内部：面板来源 ───────────────────────────────────────────
    @staticmethod
    def _skill_panels(skill_spec: dict[str, Any]) -> list[dict[str, Any]]:
        """从技能定义取面板模板（``panels`` 或 ``steps_json`` 字段，可能是 JSON 字符串）。"""
        raw = skill_spec.get("panels") or skill_spec.get("steps_json") or []
        if isinstance(raw, str):
            raw = loads_or(raw, [])
        if isinstance(raw, list):
            return [dict(p) for p in raw if isinstance(p, dict)]
        return []

    @staticmethod
    def _parse_llm_panels(raw_text: str) -> list[dict[str, Any]]:
        """解析 LLM 返回的大屏 JSON，取出 ``panels`` 列表。

        输入：模型原始输出（可能夹带 ```json 围栏或解释文字）。
        输出：面板字典列表；结构非法 / 无 ``panels`` 时返回空列表（由上层回退到自动布局）。
        """
        data = loads_object(raw_text)
        panels = data.get("panels") if isinstance(data, dict) else None
        if isinstance(panels, list):
            return [dict(p) for p in panels if isinstance(p, dict)]
        return []

    @staticmethod
    def _auto_panels(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """按结果字段自动出图（模板/LLM 都未给面板时的兜底）。"""
        rows: list[dict[str, Any]] = list(payload.get("rows") or [])
        columns = payload.get("columns") or DashboardSpecBuilder._columns_from_rows(rows)
        if not columns:
            return [{"panel_id": "p1", "kind": "text", "title": "无可用字段"}]
        fields = [c["name"] for c in columns]
        time_field = next((c["name"] for c in columns if c.get("type") == "date"), None)
        num_field = next((c["name"] for c in columns if c.get("type") == "number"), None)
        if time_field and num_field:
            return [
                {
                    "panel_id": "p1",
                    "kind": "chart",
                    "title": payload.get("question") or "趋势",
                    "chart": {"type": "line"},
                    "encoding": {
                        "x": {"field": time_field, "type": "time"},
                        "y": [{"field": num_field, "agg": "sum"}],
                    },
                }
            ]
        return [
            {
                "panel_id": "p1",
                "kind": "table",
                "title": payload.get("question") or "结果",
                "chart": None,
                "encoding": {"y": [{"field": f, "agg": "sum"} for f in fields[:3]]},
            }
        ]


def _looks_like_date(text: str) -> bool:
    head = text[:10]
    return len(head) == 10 and head[4] == "-" and head[7] == "-"
