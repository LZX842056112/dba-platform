"""``/api/v1/stream``（SSE 订阅，★ P0-7 / §7.6 / §9.3）。

对齐《设计文档 v2》§7.3 / §7.6 / §9.3 与《实现要点清单》§1.5、§7.8。

三条硬约束
----------
1. **每条消息都写 ``id: {seq}``**：浏览器才会在重连时自动带 ``Last-Event-ID``；
2. **从 ``run:progress`` 回放最近 200 条**（Redis LIST，TTL 1h）——跨副本可回放；
3. **``Last-Event-ID`` 续传不丢不重**：回放 ``after_seq = Last-Event-ID``，只发更大 seq。

★ 为什么用 ticket 而不是 JWT：``EventSource`` 不支持自定义请求头（见 ``api.stream_tickets``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
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

    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
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
    """SSE 生成器：先回放（after_seq=Last-Event-ID），再轮询新事件，附 15s 心跳。"""
    sent = after
    started = asyncio.get_running_loop().time()
    last_beat = started

    # 1) 回放历史（去重靠 after_seq；seq 单调，天然不重）
    try:
        replay = await publisher.replay(trace_id, after_seq=after)
    except Exception as exc:  # noqa: BLE001 - 通道故障只影响本次订阅
        logger.warning("进度回放失败：%s", exc)
        replay = []
    for frame in build_sse_frames(replay):
        yield frame
    if replay:
        sent = max(sent, max(int(e.seq) for e in replay))

    # 2) 轮询新事件直到终态 / 超时
    while True:
        now = asyncio.get_running_loop().time()
        if now - started > _MAX_STREAM_S:
            yield _heartbeat("stream_timeout")
            return
        try:
            events = await publisher.replay(trace_id, after_seq=sent)
        except Exception:  # noqa: BLE001
            events = []
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
