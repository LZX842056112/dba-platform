"""ES ``EventIndexRepo`` 实现（Protocol 见 ``storage/protocols.py``）。

★ P1-9：``dynamic: strict`` 下写入不匹配字段会抛异常。本 Repo **不吞异常**——
  由上层（``capabilities.telemetry``）捕获后落**本地 DLQ 文件**并计数
  ``telemetry_dropped_total``，绝不静默丢弃。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .client import EsStorage

__all__ = ["EventIndexRepo"]

logger = logging.getLogger("dba.storage.es")


class EventIndexRepo:
    def __init__(self, storage: EsStorage) -> None:
        self._storage = storage

    async def ensure_indices(self) -> None:
        await self._storage.ensure_indices()

    async def index_run_event(self, doc: dict[str, Any]) -> None:
        await self._storage.index(self._storage.alias("run-event"), self._stamp(doc))

    async def index_sql_audit(self, doc: dict[str, Any]) -> None:
        await self._storage.index(self._storage.alias("sql-audit"), self._stamp(doc))

    async def index_metric_raw(self, doc: dict[str, Any]) -> None:
        await self._storage.index(self._storage.alias("metric-raw"), self._stamp(doc))

    async def write(self, record: dict[str, Any]) -> None:
        """审计落点统一入口（``AuditMiddleware`` 经由容器 ``audit_sink`` 调用）。

        ★ 契约补齐：容器把 ``audit_sink`` 设为本 Repo（``di.py``），而 ``AuditMiddleware``
        调的是 ``sink.write(record)``；本类此前只有 ``index_*`` 三个具名方法、**没有
        ``write``** → 任意非 skip 请求抛 ``AttributeError``。这里把 HTTP 请求审计委托给
        ``index_run_event``（写入 ``run-event`` 别名），与 ```` 的语义一致。
        """
        await self.index_run_event(record)

    async def search(self, alias: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._storage.search(self._storage.alias(alias), body)

    @staticmethod
    def _stamp(doc: dict[str, Any]) -> dict[str, Any]:
        """补 ``@timestamp``（strict 映射要求存在，写入方可能未给）。"""
        return {"@timestamp": int(time.time() * 1000), **doc}
