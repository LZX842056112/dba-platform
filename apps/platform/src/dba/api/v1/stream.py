"""``/api/v1/stream``（SSE 订阅，★ P0-7 / §7.6 / §9.3）。

对齐《设计文档 v2》§7.3 / §7.6 / §9.3 与《实现要点清单》§1.5、§7.8。

三条硬约束
----------
1. **每条消息都写 ``id: {seq}``**：浏览器才会在重连时自动带 ``Last-Event-ID``；
2. **从 ``run:progress`` 回放最近 200 条**（Redis LIST，TTL 1h）——跨副本可回放；
3. **旧游标显式报缺口**：历史超出保留范围时发送 ``replay.gap``，并继续回放可用事件。

★ 为什么用 ticket 而不是 JWT：``EventSource`` 不支持自定义请求头（见 ``api.stream_tickets``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from dba.api.deps import Principal, get_container, get_current_principal
from dba.di import Container

__all__ = ["router", "build_sse_frames"]

logger = logging.getLogger("dba.api.stream")

router = APIRouter()

#: 终态事件（收到即结束流）
_TERMINAL_EVENTS = frozenset({"run.finished", "run.error", "run.aborted"})

#: 心跳间隔（秒）
_HEARTBEAT_S = 15.0

#: 单次订阅的最长时长（秒），避免连接悬挂
_MAX_STREAM_S = 300.0


@dataclass(frozen=True)
class _ReplayPage:
    """兼容旧进度发布器的回放页；旧协议无法提供缺口元数据。"""

    events: list[Any]
    oldest_seq: int | None
    latest_seq: int | None
    has_gap: bool = False


def build_sse_frames(events: list[Any]) -> list[str]:
    """把回放事件渲染为 SSE 帧（``id: {seq}`` + ``event`` + ``data``）。

    ★ 纯函数，便于对「不丢不重」做单测：``seq`` 严格取事件自身 seq，不重编号。
    """
    frames: list[str] = []
    for event in events:
        data = json.dumps(event.data, ensure_ascii=False)
        frames.append(f"id: {event.seq}\nevent: {event.event}\ndata: {data}\n\n")
    return frames


@router.post("/ticket", response_model=None)
async def issue_ticket(
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /stream/ticket``：签发订阅票据（★ P0-7）。"""
    trace_id = str(payload.get("trace_id") or "")
    if not trace_id:
        return JSONResponse(status_code=400, content={"code": "40001", "message": "缺少 trace_id"})
    now = datetime.now(UTC).replace(tzinfo=None)
    since = _parse_time(payload.get("since")) or (now - timedelta(days=1))
    until = _parse_time(payload.get("until")) or (now + timedelta(days=1))
    tickets = container.get("stream_tickets")
    ticket = tickets.issue(trace_id=trace_id, user_id=principal.user_id, since=since, until=until)
    return {"stream_ticket": ticket.ticket, "expires_in": 60}


@router.get("/runs/{trace_id}")
async def stream_run(
    request: Request,
    trace_id: str,
    container: Annotated[Container, Depends(get_container)],
    ticket: Annotated[str | None, Query()] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> Any:
    """``GET /stream/runs/{trace_id}?ticket=``：订阅某次 Run 的实时事件。"""
    tickets = container.get("stream_tickets")
    record = tickets.verify(ticket or "", trace_id=trace_id) if tickets is not None else None
    if record is None:
        return JSONResponse(
            status_code=401,
            content={
                "code": "40100",
                "message": "ticket 无效或已过期",
                "detail": {},
                "trace_id": None,
            },
        )

    publisher = container.get("progress_publisher")
    if publisher is None:
        return JSONResponse(
            status_code=503,
            content={
                "code": "50301",
                "message": "进度通道不可用",
                "detail": {"component": "redis"},
                "trace_id": None,
            },
        )

    cursor = last_event_id if last_event_id and last_event_id.isdigit() else last_event_id_query
    after = int(cursor) if cursor and cursor.isdigit() else 0
    return StreamingResponse(
        _stream(publisher, trace_id, after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 Nginx 缓冲，否则 SSE 会被攒批
        },
    )


async def _stream(publisher: Any, trace_id: str, after: int) -> AsyncIterator[str]:
    """SSE 生成器：先报告保留窗口缺口，再回放新游标之后的事件并轮询。"""
    sent = after
    started = asyncio.get_running_loop().time()
    last_beat = started

    # 1) 读取游标窗口；老于首条保留事件时显式提示，绝不把截断伪装成完整续传。
    try:
        page = await _read_replay_page(publisher, trace_id, after)
    except Exception as exc:  # noqa: BLE001 - 通道故障只影响本次订阅
        logger.warning("进度回放失败：%s", exc)
        page = None
    if page is not None and page.has_gap:
        yield _replay_gap_frame(after, page)
        if not page.events and page.latest_seq is not None:
            # 列表 TTL 已到但水位仍在；推进服务端游标以等待未来的新事件。
            sent = page.latest_seq
    replay = page.events if page is not None else []
    for frame in build_sse_frames(replay):
        yield frame
    if replay:
        sent = max(sent, max(int(e.seq) for e in replay))
        # 重连时终态可能已经在历史窗口中；回放完终态必须关闭连接，避免空轮询。
        if any(event.event in _TERMINAL_EVENTS for event in replay):
            return

    # 2) 轮询新事件直到终态 / 超时
    while True:
        now = asyncio.get_running_loop().time()
        if now - started > _MAX_STREAM_S:
            yield _heartbeat("stream_timeout")
            return
        try:
            page = await _read_replay_page(publisher, trace_id, sent)
        except Exception:  # noqa: BLE001
            page = None
        events = page.events if page is not None else []
        if page is not None and page.has_gap:
            yield _replay_gap_frame(sent, page)
            if not events and page.latest_seq is not None:
                sent = page.latest_seq
        if events:
            for frame in build_sse_frames(events):
                yield frame
            sent = max(int(e.seq) for e in events)
            if any(e.event in _TERMINAL_EVENTS for e in events):
                return
            continue
        if now - last_beat >= _HEARTBEAT_S:
            yield _heartbeat("heartbeat")
            last_beat = now
        await asyncio.sleep(0.5)


def _replay_gap_frame(after: int, page: Any) -> str:
    """构造缺口提示；无可回放事件时用水位作为 id，避免客户端反复从旧游标重连。"""
    payload = json.dumps(
        {
            "after_seq": after,
            "oldest_available_seq": page.oldest_seq,
            "latest_seq": page.latest_seq,
            "advance_cursor": not bool(page.events),
        },
        ensure_ascii=False,
    )
    cursor = f"id: {page.latest_seq}\n" if not page.events and page.latest_seq is not None else ""
    return f"{cursor}event: replay.gap\ndata: {payload}\n\n"


async def _read_replay_page(publisher: Any, trace_id: str, after: int) -> Any:
    """读取有缺口信息的新协议页，或兼容仅支持 replay() 的旧发布器。"""
    page_reader = getattr(publisher, "replay_page", None)
    if page_reader is not None:
        return await page_reader(trace_id, after_seq=after)
    events = await publisher.replay(trace_id, after_seq=after)
    seqs = [int(event.seq) for event in events]
    return _ReplayPage(
        events=events,
        oldest_seq=min(seqs, default=None),
        latest_seq=max(seqs, default=None),
    )


def _heartbeat(kind: str) -> str:
    """SSE 心跳帧（``event: heartbeat``，不带 ``id``，不参与 seq 连续性）。"""
    payload = json.dumps({"kind": kind, "ts_ms": int(datetime.now(UTC).timestamp() * 1000)})
    return f"event: heartbeat\ndata: {payload}\n\n"


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            return None
    return None
