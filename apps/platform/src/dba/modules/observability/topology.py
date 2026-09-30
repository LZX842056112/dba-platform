"""模块 03 · 调用拓扑聚合（§6.4 ``TopologyBuilder``）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§3.22、§4.0 归属矩阵。

拓扑从哪里来
------------
``run_doc.spans[]`` 里每个 span 都带 ``span_id`` / ``parent_span_id`` / ``name`` / ``kind``。
把「Agent 步骤 span」与其子 span（工具 / 子 Agent / LLM）的父子关系聚合，就得到
「Agent → 工具 / 子 Agent」的调用拓扑。改一个 Agent 的调用链，拓扑应随之变化
（§12.5 的可验证产出①）。

★ 归属约束（§5.9）：``run_doc`` 的 owner 是 L4 ``MeteringService``，因此本模块
**不直连 Mongo**，而是经 ``MeteringService.get_run_doc(trace_id)`` 读取。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TopologyNode", "TopologyEdge", "TopologyGraph", "TopologyBuilder"]

logger = logging.getLogger("dba.modules.observability.topology")

#: 参与拓扑的 span kind（只看 Agent/工具/子 Agent，忽略 LLM 细粒度调用以免图过密）
_TOPOLOGY_KINDS: frozenset[str] = frozenset({"agent", "tool", "sub_agent"})


@dataclass(frozen=True, slots=True)
class TopologyNode:
    """拓扑节点。"""

    node_id: str
    name: str
    kind: str
    span_count: int = 0
    error_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "kind": self.kind,
            "span_count": self.span_count,
            "error_count": self.error_count,
        }


@dataclass(frozen=True, slots=True)
class TopologyEdge:
    """拓扑边（``source → target``）。"""

    source: str
    target: str
    calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "calls": self.calls}


@dataclass(slots=True)
class TopologyGraph:
    """拓扑图（``GET /obs/topology`` 的返回）。"""

    nodes: list[TopologyNode] = field(default_factory=list)
    edges: list[TopologyEdge] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.as_dict() for n in self.nodes],
            "edges": [e.as_dict() for e in self.edges],
        }


class TopologyBuilder:
    """从 ``run_doc`` 的 span 父子关系聚合拓扑。"""

    def __init__(self, *, metering: Any | None = None, run_repo: Any | None = None) -> None:
        self._metering = metering
        self._run_repo = run_repo

    async def build(self, biz_line_id: int | None) -> TopologyGraph:
        """聚合某业务线近期 Run 的拓扑。"""
        docs = await self._load_run_docs(biz_line_id)
        return self.build_from_docs(docs)

    @staticmethod
    def build_from_docs(docs: list[dict[str, Any]]) -> TopologyGraph:
        """纯函数：从 ``run_doc`` 列表聚合拓扑（便于单测）。"""
        node_names: dict[str, str] = {}
        node_kinds: dict[str, str] = {}
        node_spans: Counter[str] = Counter()
        node_errors: Counter[str] = Counter()
        edges: Counter[tuple[str, str]] = Counter()

        for doc in docs:
            spans = list(doc.get("spans") or [])
            by_id = {str(s.get("span_id")): s for s in spans}
            for span in spans:
                kind = str(span.get("kind") or "")
                if kind not in _TOPOLOGY_KINDS:
                    continue
                span_id = str(span.get("span_id"))
                node_names[span_id] = str(span.get("name") or span_id)
                node_kinds[span_id] = kind
                node_spans[span_id] += 1
                if str(span.get("status")) == "error":
                    node_errors[span_id] += 1

                parent_id = span.get("parent_span_id")
                if not parent_id:
                    continue
                parent = by_id.get(str(parent_id))
                if parent is None:
                    continue
                if str(parent.get("kind") or "") not in _TOPOLOGY_KINDS:
                    continue
                edges[(str(parent_id), span_id)] += 1

        nodes = [
            TopologyNode(
                node_id=node_id,
                name=node_names[node_id],
                kind=node_kinds[node_id],
                span_count=node_spans[node_id],
                error_count=node_errors[node_id],
            )
            for node_id in sorted(node_names)
        ]
        edge_list = [
            TopologyEdge(source=src, target=dst, calls=calls)
            for (src, dst), calls in sorted(edges.items())
        ]
        return TopologyGraph(nodes=nodes, edges=edge_list)

    async def _load_run_docs(self, biz_line_id: int | None) -> list[dict[str, Any]]:
        """加载近期 Run 的 ``run_doc``（经 owner 的读取接口，不直连 Mongo）。"""
        if self._run_repo is None or self._metering is None:
            return []
        try:
            runs = await self._run_repo.list_runs(
                {"biz_line_id": biz_line_id, "limit": 50}, limit=50
            )
        except Exception as exc:  # noqa: BLE001 - 拓扑可降级为空
            logger.warning("读取 run 列表失败（拓扑降级为空）：%s", exc)
            return []

        docs: list[dict[str, Any]] = []
        for run in runs:
            trace_id = str(run.get("trace_id") or "")
            if not trace_id:
                continue
            try:
                doc = await self._metering.get_run_doc(trace_id)
            except Exception:  # noqa: BLE001
                doc = None
            if doc:
                docs.append(dict(doc))
        return docs
