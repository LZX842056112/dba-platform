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
    SpecMeta,
)

__all__ = ["DashboardSpecBuilder"]

logger = logging.getLogger("dba.modules.chatbi.spec_builder")

#: 内联行数阈值（超过则改走 s3）
INLINE_ROW_THRESHOLD = 500
#: 每个面板占位（栅格 12 列；默认两列布局）
_PANEL_W = 6
_PANEL_H = 8


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
        """
        rows: list[dict[str, Any]] = list(payload.get("rows") or [])
        columns: list[dict[str, str]] = list(payload.get("columns") or [])
        for source in spec.data_sources:
            if source.mode != "s3" or source.presigned_url:
                continue
            if self._object_store is None:
                logger.warning("需 s3 数据源但对象存储不可用，object_key=%s", source.object_key)
                continue
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
    def _assemble(
        self,
        payload: dict[str, Any],
        panel_dicts: list[dict[str, Any]],
        ctx: RunContext | None,
    ) -> DashboardSpec:
        rows: list[dict[str, Any]] = list(payload.get("rows") or [])
        columns = payload.get("columns") or self._columns_from_rows(rows)
        scope_hash = payload.get("scope_hash")
        biz_line_id = payload.get("biz_line_id")
        metric_codes = list(payload.get("metric_codes") or [])

        panels: list[Panel] = []
        items: list[LayoutItem] = []
        for idx, raw in enumerate(panel_dicts):
            panel_id = str(raw.get("panel_id") or f"p{idx + 1}")
            x = (idx % 2) * _PANEL_W
            y = (idx // 2) * _PANEL_H
            items.append(LayoutItem(i=panel_id, x=x, y=y, w=_PANEL_W, h=_PANEL_H, minW=3, minH=5))
            panels.append(
                self._panel(panel_id, raw, {"ref": "q1"}, metric_codes, biz_line_id, scope_hash)
            )

        source = self._data_source(rows, columns, payload)
        trace_id = ctx.trace_id if ctx is not None else str(payload.get("trace_id") or "")
        return DashboardSpec(
            dashboard_id="dsh_" + new_ulid(),
            version=1,
            title=str(payload.get("title") or payload.get("question") or "数据大屏"),
            layout=Layout(items=items),
            panels=panels,
            data_sources=[source],
            meta=SpecMeta(
                generated_by_trace=trace_id,
                generated_at=dt.datetime.now(dt.UTC).isoformat(),
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
        chart = raw.get("chart") or {}
        encoding = raw.get("encoding")
        return Panel(
            panel_id=panel_id,
            kind=raw.get("kind", "chart"),
            title=str(raw.get("title") or panel_id),
            subtitle=raw.get("subtitle"),
            chart=ChartSpec(type=chart.get("type", "line")) if chart else None,
            encoding=Encoding.model_validate(encoding) if isinstance(encoding, dict) else None,
            dataset=dataset,
            query=PanelQuery(
                metric_codes=list(raw.get("metric_codes") or metric_codes),
                biz_line_id=biz_line_id,
                scope_hash=scope_hash,
            ),
            interaction=dict(raw.get("interaction") or {}),
            style=dict(raw.get("style") or {}),
        )

    def _data_source(
        self, rows: list[dict[str, Any]], columns: list[dict[str, str]], payload: dict[str, Any]
    ) -> DataSource:
        if len(rows) > self._threshold:
            return DataSource(
                ref="q1",
                mode="s3",
                object_key=f"{payload.get('dashboard_key', 'dsh')}/q1.json",
                row_count=len(rows),
                format="json",
            )
        matrix = [[row.get(col["name"]) for col in columns] for row in rows]
        return DataSource(
            ref="q1",
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
        raw = skill_spec.get("panels") or skill_spec.get("steps_json") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return []
        if isinstance(raw, list):
            return [dict(p) for p in raw if isinstance(p, dict)]
        return []

    @staticmethod
    def _parse_llm_panels(raw_text: str) -> list[dict[str, Any]]:
        text = raw_text.strip()
        if not text:
            return []
        # 容错：剥离 ```json 围栏，取第一个 { ... } 块
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                candidate = part.strip()
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith("{"):
                    text = candidate
                    break
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return []
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return []
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
