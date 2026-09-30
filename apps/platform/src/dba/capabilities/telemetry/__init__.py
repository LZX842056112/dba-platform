"""埋点与投递能力（owner: capabilities.telemetry）。

对齐《设计文档 v2》§6.1.4、§5.6、§5.7 与《实现要点清单》P1-6 / P1-9。
"""

from __future__ import annotations

from .dlq import DlqRecord, LocalDlqWriter
from .metering import OutboxDispatcher, OutboxMeteringService

__all__ = ["DlqRecord", "LocalDlqWriter", "OutboxDispatcher", "OutboxMeteringService"]
