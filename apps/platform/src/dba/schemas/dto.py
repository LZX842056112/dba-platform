"""ChatBI 请求 / 响应 DTO（API 层使用）。

对齐《设计方案 v2》§7.3（模块 01 · ChatBI 接口清单）。

★ v2：``POST /chat/sessions/{sid}/query`` **不再直接返回 SSE 流**，而是返回
``{trace_id, stream_ticket, expires_in}``——因为 ``EventSource`` 无法携带
``Authorization`` 头，必须改用「先 POST 拿一次性 ticket，再 ``?ticket=`` 打开 SSE」。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "QueryOptions",
    "QueryRequest",
    "QueryAccepted",
    "SessionCreateOut",
    "ChatMessageOut",
    "SqlAuditOut",
]


class QueryOptions(BaseModel):
    """查询可选项（来自请求体 ``options``）。"""

    chart_hint: str | None = None
    max_rows: int = Field(default=5000, ge=1, le=100_000)
    stream: bool = True


class QueryRequest(BaseModel):
    """``POST /chat/sessions/{sid}/query`` 请求体。"""

    question: str = Field(min_length=1, max_length=4000)
    options: QueryOptions = Field(default_factory=QueryOptions)


class QueryAccepted(BaseModel):
    """查询已受理（★ v2：返回 ticket，而非流本身）。"""

    trace_id: str
    stream_ticket: str
    expires_in: int


class SessionCreateOut(BaseModel):
    """新建会话响应。"""

    session_id: str


class ChatMessageOut(BaseModel):
    """会话消息（Mongo ``chat_message``）。"""

    seq: int
    role: Literal["user", "assistant", "system"]
    content: str
    trace_id: str | None = None
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SqlAuditOut(BaseModel):
    """SQL 审计条目（``GET /chat/sql-audit``）。"""

    id: int
    trace_id: str
    user_id: int | None = None
    biz_line_id: int | None = None
    sql_fingerprint: str
    sql_text: str
    rewritten_sql: str | None = None
    decision: Literal["allow", "rewrite", "deny", "retry"]
    deny_reason: str | None = None
    guard_stage: str | None = None
    scope_injected: bool = False
    scope_hash: str | None = None
    rows_returned: int | None = None
    exec_ms: int | None = None
    created_at: str | None = None
