"""消息与进度能力（owner: capabilities.messaging）。

对齐《设计文档 v2》§5.6 / §7.4 与《实现要点清单》§5.6。
"""

from __future__ import annotations

from .progress import ProgressEvent, ProgressPublisher

__all__ = ["ProgressEvent", "ProgressPublisher"]
