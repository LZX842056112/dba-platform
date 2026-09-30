"""模块 03 · OTLP/HTTP Span 接入（§6.4 ``OtelSpanIngester`` / §7.4 ``POST /obs/otel/v1/traces``）。

对齐《设计方案 v2》§6.4、§7.4 与《实现要点清单》§3.23。

为什么要支持 OTLP 接入
----------------------
``app_agent.runtime_type`` 里有 ``external_sdk`` / ``otel_agent`` 两类：这些 Agent 不在
本平台进程内，只能**自报** token 与成本（``usage_source=self_reported``）。所以需要一个
标准的 OTLP 端点把它们的 span 收进来，与内部 Run 用同一套 ``run_doc`` / ``span`` 结构呈现。

★ 诚实标注（不许假过）
----------------------
* **OTLP/JSON 已实现**（``Content-Type: application/json``）；这是本批次可端到端验证的路径。
* **OTLP/protobuf 未实现**：需 ``opentelemetry-proto`` 生成代码（额外依赖），本批次标注
  ``TODO(protobuf)`` 并在报告「遗留问题」中列出，**不假装支持**。
* 自报数据一律标 ``usage_source=self_reported``，不参与预算扣减（§13 追问 8）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

__all__ = ["OtelIngestResult", "OtelSpanIngester"]

logger = logging.getLogger("dba.modules.observability.otel")

#: OTLP span 的子集字段 → 本平台 span 记录字段的映射
_SPAN_KIND_MAP: dict[int, str] = {
    1: "internal",
    2: "server",
    3: "client",
    4: "producer",
    5: "consumer",
}

#: OTLP status.code：0=UNSET 1=OK 2=ERROR
_STATUS_MAP: dict[int, str] = {0: "ok", 1: "ok", 2: "error"}


@dataclass(slots=True)
class OtelIngestResult:
    """接入结果（对应 OTLP 的 ``{partialSuccess}``）。"""

    accepted: int = 0
    rejected: int = 0
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"partialSuccess": {"rejectedSpans": self.rejected}}
        if self.messages:
            body["partialSuccess"]["errorMessage"] = "; ".join(self.messages[:5])
        return body


def _attr_value(value: dict[str, Any]) -> Any:
    """把 OTLP ``AnyValue`` 归一为 Python 标量。"""
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            raw = value[key]
            if key in ("intValue", "doubleValue"):
                try:
                    return float(raw) if key == "doubleValue" else int(raw)
                except (TypeError, ValueError):
                    return raw
            return raw
    if "arrayValue" in value:
        items = value["arrayValue"].get("values") or []
        return [_attr_value(item) for item in items]
    return None


def _attrs_to_dict(attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """OTLP ``KeyValue[]`` → ``{key: value}``。"""
    out: dict[str, Any] = {}
    for item in attributes or []:
        key = item.get("key")
        if not key:
            continue
        out[str(key)] = _attr_value(item.get("value") or {})
    return out


class OtelSpanIngester:
    """OTLP Span 接入器（JSON）。

    收到的 span 经注入的 ``metering``（L4 ``MeteringService``）落 ``run_doc.spans[]``，
    与内部 Run 用同一结构——这样 ``TopologyBuilder`` 对内外一视同仁。
    """

    def __init__(self, *, metering: Any | None = None) -> None:
        self._metering = metering

    def parse(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], OtelIngestResult]:
        """把 OTLP/JSON 解析为本平台 span 记录列表。"""
        result = OtelIngestResult()
        spans: list[dict[str, Any]] = []
        for resource_span in payload.get("resourceSpans") or []:
            resource_attrs = _attrs_to_dict((resource_span.get("resource") or {}).get("attributes"))
            for scope_span in resource_span.get("scopeSpans") or []:
                for raw in scope_span.get("spans") or []:
                    record = self._convert(raw, resource_attrs)
                    if record is None:
                        result.rejected += 1
                        result.messages.append("span 缺少 traceId/spanId，已拒绝")
                        continue
                    spans.append(record)
                    result.accepted += 1
        return spans, result

    @staticmethod
    def _convert(raw: dict[str, Any], resource_attrs: dict[str, Any]) -> dict[str, Any] | None:
        """单条 OTLP span → 平台 span 记录。"""
        trace_id = str(raw.get("traceId") or "")
        span_id = str(raw.get("spanId") or "")
        if not trace_id or not span_id:
            return None
        start_ns = int(raw.get("startTimeUnixNano") or 0)
        end_ns = int(raw.get("endTimeUnixNano") or 0)
        status_code = int((raw.get("status") or {}).get("code") or 0)
        attrs = _attrs_to_dict(raw.get("attributes"))
        return {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": str(raw.get("parentSpanId") or "") or None,
            "name": str(raw.get("name") or "otel.span"),
            "kind": attrs.get("dba.kind")
            or _SPAN_KIND_MAP.get(int(raw.get("kind") or 1), "internal"),
            "start_ms": start_ns // 1_000_000,
            "duration_ms": max(0, (end_ns - start_ns) // 1_000_000),
            "status": _STATUS_MAP.get(status_code, "ok"),
            "attributes": attrs,
            "resource": resource_attrs,
            # ★ 外部 Agent 自报：标 self_reported，不参与预算扣减
            "usage_source": "self_reported",
        }

    async def ingest(self, payload: dict[str, Any]) -> OtelIngestResult:
        """接入并落库。protobuf 未支持（见模块 docstring 的诚实标注）。"""
        spans, result = self.parse(payload)
        if self._metering is None:
            return result
        for span in spans:
            try:
                await self._metering.record_span(span)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批（partialSuccess）
                result.rejected += 1
                result.accepted -= 1
                result.messages.append(f"{span.get('span_id')}: {exc}")
        return result

    async def ingest_protobuf(self, payload: bytes) -> OtelIngestResult:
        """OTLP/protobuf 接入。

        TODO(protobuf)：需要 ``opentelemetry-proto`` 生成代码（额外依赖）。本批次
        明确不支持，返回 415 语义的结果，**不伪造成功**。
        """
        _ = payload
        return OtelIngestResult(
            accepted=0,
            rejected=0,
            messages=["OTLP/protobuf 未实现（TODO）：请使用 Content-Type: application/json"],
        )
