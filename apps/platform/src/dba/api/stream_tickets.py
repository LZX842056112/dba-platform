"""一次性 SSE 订阅票据（★ P0-7）。

对齐《设计文档 v2》§7.3 与《实现要点清单》§1.5、§7.8。

为什么需要 ticket（照抄 v1 会怎样错）
------------------------------------
全局鉴权约定是 ``Authorization: Bearer <JWT>``，而浏览器原生 ``EventSource``
**不支持自定义请求头**——`new EventSource(url, {withCredentials:true})` 根本带不上 JWT，
v1 的 SSE 组合从一开始就无法通过鉴权。v2 改为「先 ``POST`` 拿一次性 ticket，再用
``?ticket=`` 打开 SSE」。

★ 三条硬约束（缺一条都会让重连失败或串权）
----------------------------------------
1. **绑 ``(trace_id, user_id)``**：ticket 只能订阅它绑定的那一个 Run；
2. **TTL 60s 内可复用**：SSE 断线会复用同一个 URL，做成「一次性消费」会导致重连必然失败；
3. **固化时间窗 ``since/until``**：``GET /runs/{trace_id}`` 受分区表限制，必须带时间窗。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = ["StreamTicket", "StreamTicketStore"]


@dataclass(frozen=True, slots=True)
class StreamTicket:
    """一张订阅票据。"""

    ticket: str
    trace_id: str
    user_id: int | None
    since: datetime
    until: datetime
    issued_at: datetime
    expires_at: datetime

    def is_expired(self, at: datetime | None = None) -> bool:
        moment = at or datetime.now(UTC).replace(tzinfo=None)
        return moment >= self.expires_at


class StreamTicketStore:
    """票据存储（进程内；跨副本需 Redis，见报告遗留问题）。

    ★ 诚实标注：本实现是**进程内**的，多副本部署下「A 副本签发、B 副本校验」会失败。
    生产应落 Redis（键 ``stream:ticket:{ticket}``，TTL 60s）。已在报告登记。
    """

    def __init__(self, *, ttl_s: int = 60) -> None:
        self._ttl_s = ttl_s
        self._tickets: dict[str, StreamTicket] = {}

    def issue(
        self,
        *,
        trace_id: str,
        user_id: int | None,
        since: datetime,
        until: datetime,
    ) -> StreamTicket:
        """签发票据（``st_`` 前缀，:func:`secrets.token_urlsafe`）。"""
        now = datetime.now(UTC).replace(tzinfo=None)
        ticket = f"st_{secrets.token_urlsafe(24)}"
        record = StreamTicket(
            ticket=ticket,
            trace_id=trace_id,
            user_id=user_id,
            since=since,
            until=until,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl_s),
        )
        self._tickets[ticket] = record
        self._evict_expired(now)
        return record

    def verify(
        self, ticket: str, *, trace_id: str | None = None, user_id: int | None = None
    ) -> StreamTicket | None:
        """校验票据；TTL 内**可重复**使用（绑定的 trace/user 必须匹配）。"""
        record = self._tickets.get(ticket)
        if record is None or record.is_expired():
            return None
        if trace_id is not None and record.trace_id != trace_id:
            return None
        if user_id is not None and record.user_id is not None and record.user_id != user_id:
            return None
        return record

    def _evict_expired(self, now: datetime) -> None:
        stale = [key for key, rec in self._tickets.items() if rec.is_expired(now)]
        for key in stale:
            self._tickets.pop(key, None)
