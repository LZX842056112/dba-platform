"""本地 DLQ（Dead Letter Queue）写入器。

对齐《设计文档 v2》§5.7 与《实现要点清单》P1-9。

★ 为什么必须有 DLQ（照抄 v1 会怎样错）
------------------------------------
ES 索引是 ``dynamic: strict``：写入未声明字段会**抛异常**。v1 在投递失败时直接
``except: pass``（或只打日志）——于是「映射不匹配」的埋点被**静默丢弃**，成本/审计缺数，
却无人知晓。v2 要求：投递失败 → 落**本地 DLQ 文件**（JSONL，可回放）+ 计数
``telemetry_dropped_total``，做到「宁可落盘、绝不静默丢」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["DlqRecord", "LocalDlqWriter"]

logger = logging.getLogger("dba.capabilities.telemetry.dlq")


@dataclass
class DlqRecord:
    """一条 DLQ 记录。"""

    kind: str
    error: str
    payload: dict[str, Any] = field(default_factory=dict)


class LocalDlqWriter:
    """把投递失败的记录以 JSONL 追加落盘（线程安全、异步友好）。

    * 落盘路径默认 ``./var/dlq/telemetry.jsonl``（可由 ``DBA_DLQ_PATH`` 覆盖）；
    * ``dropped_total`` 计数导出为 ``telemetry_dropped_total`` 指标；
    * ``replay()`` 可读回全部记录，供人工/定时补投。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        env_path = os.environ.get("DBA_DLQ_PATH")
        self._path = Path(path or env_path or "var/dlq/telemetry.jsonl")
        self._lock = threading.Lock()
        self.dropped_total = 0

    @property
    def path(self) -> Path:
        return self._path

    async def write(self, kind: str, error: str, payload: dict[str, Any]) -> None:
        """异步写一条 DLQ；落盘在**线程池**执行，不阻塞事件循环。"""
        await asyncio.to_thread(self.write_sync, kind, error, payload)

    def write_sync(self, kind: str, error: str, payload: dict[str, Any]) -> None:
        line = json.dumps(
            {"kind": kind, "error": error, "payload": payload}, ensure_ascii=False, default=str
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.dropped_total += 1

    def replay(self) -> list[DlqRecord]:
        """读回所有 DLQ 记录（供补投）。"""
        if not self._path.exists():
            return []
        out: list[DlqRecord] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover
                    logger.warning("跳过损坏的 DLQ 行")
                    continue
                out.append(
                    DlqRecord(
                        kind=str(raw.get("kind", "")),
                        error=str(raw.get("error", "")),
                        payload=dict(raw.get("payload") or {}),
                    )
                )
        return out

    def clear(self) -> None:
        with self._lock:
            if self._path.exists():
                self._path.unlink()
