"""运行进度发布（SSE）。

对齐《设计文档 v2》§5.6 / §7.4 与《实现要点清单》§5.6。

★ 为什么进度流要落 Redis（照抄 v1 会怎样错）
------------------------------------------
v1 用进程内 list 保存 ``run:progress``。问题是：SSE 客户端断线重连时，
若负载均衡把重连打到**另一个副本**，该副本读不到原副本写入的进度 →
前端「进度丢失 / 从 0 重放」。v2 把进度写 Redis LIST（保留最近 200 条、TTL 1h），
任意副本都能 ``replay``，SSE 断线续传才成立。

★ ``seq`` 单调递增：前端按 ``Last-Event-ID`` 续传（§7.4），缺 ``seq`` 无法去重。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ProgressEvent", "ProgressPublisher", "ProgressReplay"]


@dataclass
class ProgressEvent:
    """一条进度事件。"""

    trace_id: str
    seq: int
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    ts_ms: int = 0

    def envelope(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "seq": self.seq,
            "event": self.event,
            "data": self.data,
            "ts_ms": self.ts_ms or int(time.time() * 1000),
        }

    def to_sse(self) -> str:
        """渲染为 SSE 帧（``id`` 用 ``seq``，供前端 ``Last-Event-ID`` 续传）。"""
        import json  # noqa: PLC0415

        payload = json.dumps(self.data, ensure_ascii=False)
        return f"id: {self.seq}\nevent: {self.event}\ndata: {payload}\n\n"


@dataclass
class ProgressReplay:
    """有限保留窗口中的事件及其游标范围，用于检测 SSE 历史缺口。"""

    events: list[ProgressEvent]
    oldest_seq: int | None
    latest_seq: int | None
    has_gap: bool


class ProgressPublisher:
    """进度发布器（写 Redis；进程内另存 seq 计数器，保证单调）。"""

    def __init__(self, writer: Any) -> None:
        self._writer = writer
        self._seq: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.published_total = 0

    async def publish(
        self, trace_id: str, event: str, data: dict[str, Any] | None = None
    ) -> ProgressEvent:
        """发布一条进度事件（``seq`` 单调递增）。"""
        async with self._lock:
            seq = self._seq.get(trace_id, 0) + 1
            self._seq[trace_id] = seq
        envelope = ProgressEvent(
            trace_id=trace_id, seq=seq, event=event, data=data or {}, ts_ms=int(time.time() * 1000)
        ).envelope()
        await self._writer.write(trace_id, envelope)
        self.published_total += 1
        return ProgressEvent(
            trace_id=trace_id, seq=seq, event=event, data=data or {}, ts_ms=envelope["ts_ms"]
        )

    async def replay(self, trace_id: str, *, after_seq: int = 0) -> list[ProgressEvent]:
        """回放进度（``after_seq`` 用于断线续传）。"""
        raws = await self._writer.replay(trace_id, after_seq=after_seq)
        events: list[ProgressEvent] = []
        for raw in raws:
            events.append(
                ProgressEvent(
                    trace_id=str(raw.get("trace_id", trace_id)),
                    seq=int(raw.get("seq", 0)),
                    event=str(raw.get("event", "message")),
                    data=dict(raw.get("data") or {}),
                    ts_ms=int(raw.get("ts_ms", 0)),
                )
            )
        return events

    async def replay_page(self, trace_id: str, *, after_seq: int = 0) -> ProgressReplay:
        """读取当前保留窗口并指出调用方游标之前是否有已过期事件。"""
        page_reader = getattr(self._writer, "replay_page", None)
        if page_reader is None:
            raws = await self._writer.replay(trace_id, after_seq=0)
            latest = max((int(row.get("seq", 0)) for row in raws), default=None)
        else:
            raws, _oldest, latest = await page_reader(trace_id)
        events = [
            ProgressEvent(
                trace_id=str(raw.get("trace_id", trace_id)),
                seq=int(raw.get("seq", 0)),
                event=str(raw.get("event", "message")),
                data=dict(raw.get("data") or {}),
                ts_ms=int(raw.get("ts_ms", 0)),
            )
            for raw in raws
        ]
        oldest = min((event.seq for event in events), default=None)
        available_latest = max((event.seq for event in events), default=None)
        latest = max(
            (value for value in (latest, available_latest) if value is not None), default=None
        )
        has_gap = (
            after_seq < oldest - 1
            if oldest is not None
            else latest is not None and after_seq < latest
        )
        return ProgressReplay(
            events=[event for event in events if event.seq > after_seq],
            oldest_seq=oldest,
            latest_seq=latest,
            has_gap=has_gap,
        )

    def forget(self, trace_id: str) -> None:
        """Run 结束后释放进程内 seq 计数（Redis 侧由 TTL 清理）。"""
        self._seq.pop(trace_id, None)
