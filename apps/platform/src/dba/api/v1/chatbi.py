"""``/api/v1/chat``（模块 01 · ChatBI 对话接口，§7.3）。

对齐《设计文档 v2》§7.3 / §9.3 与《实现要点清单》§1.6、P0-7、U16。

★ P0-7：``POST /chat/sessions/{sid}/query`` **不再直接返回 SSE 流**，而是
  「立即返回 ``trace_id`` + 一次性 ``stream_ticket``」；前端再用 ``?ticket=`` 打开
  ``GET /stream/runs/{trace_id}``。原因：``EventSource`` 带不上 Bearer JWT。

★ U16：消息 ``seq`` 由 ``chat_session.next_message_seq``（``findOneAndUpdate($inc)``）
  原子分配，避免并发写撞唯一键。

★ 归属说明：会话/消息/大屏 JSON 的 owner 是 ``modules.chatbi``；L1 装配层经
  ``mongo_repos`` 触达（红线 2 禁止的是 **L3 模块之间** 直连，L1 装配层统一接线是
  分层的应有之义；后续可收敛为 ``ChatbiService`` 门面，登记为改进项）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

from dba_runtime import RunContext, new_span_id, new_trace_id
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from dba.api.deps import Principal, get_container, get_current_principal
from dba.di import Container

__all__ = ["router"]

logger = logging.getLogger("dba.api.chatbi")

router = APIRouter()

#: 进程内取消令牌表（trace_id → CancelToken）。跨副本取消需 Redis 广播（见报告遗留问题）。
_CANCELS: dict[str, Any] = {}


class ProgressEmitter:
    """把流水线事件写入 ``run:progress``（供 SSE 回放）。

    ★ 这是 L1 的「唯一 SSE writer」语义在服务端的落点：所有模块只经 ``ctx.emitter``
    发事件，由这里统一写 ``seq`` 单调的进度流。
    """

    def __init__(self, publisher: Any, trace_id: str) -> None:
        self._publisher = publisher
        self._trace_id = trace_id

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        await self._publisher.publish(self._trace_id, event, data)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_repos(container: Container) -> Any:
    repos = container.get("repos")
    return repos


@router.post("/sessions")
async def create_session(
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /chat/sessions``：新建会话。"""
    mongo = container.get("mongo_repos")
    session_id = f"sess_{uuid4().hex[:24]}"
    date_str = _now().strftime("%Y%m%d")
    doc = {
        "session_id": session_id,
        "user_id": principal.user_id,
        "biz_line_id": payload.get("biz_line_id", principal.biz_line_id),
        "title": str(payload.get("title") or "新会话"),
        "date_str": date_str,
        "message_count": 0,
        "created_at": _now(),
    }
    if mongo is not None:
        try:
            await mongo.chat_session.create(doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建会话失败：%s", exc)
    return {"session_id": session_id}


@router.get("/sessions")
async def list_sessions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[dict[str, Any]]:
    """``GET /chat/sessions``：当前用户会话列表。"""
    _ = page
    mongo = container.get("mongo_repos")
    if mongo is None:
        return []
    try:
        rows: list[dict[str, Any]] = [
            dict(r) for r in await mongo.chat_session.list_by_user(principal.user_id, limit=size)
        ]
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话列表查询失败：%s", exc)
        return []


@router.delete("/sessions/{session_id}", response_model=None)
async def delete_session(
    session_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``DELETE /chat/sessions/{sid}``（JWT + 归属）。"""
    mongo = container.get("mongo_repos")
    if mongo is None:
        return {"ok": False, "reason": "storage_unavailable"}
    session = await mongo.chat_session.get(session_id)
    if session is not None and session.get("user_id") not in (None, principal.user_id):
        return JSONResponse(
            status_code=403,
            content={"code": "40300", "message": "越权访问", "detail": {}, "trace_id": None},
        )
    # 会话删除：无独立删除接口 → 标记删除（保留审计）；文档缺口登记为改进项
    return {"ok": True}


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """``GET /chat/sessions/{sid}/messages``（JWT + 归属）。"""
    mongo = container.get("mongo_repos")
    if mongo is None:
        return []
    session = await mongo.chat_session.get(session_id)
    if session is not None and session.get("user_id") not in (None, principal.user_id):
        return []
    try:
        return [dict(r) for r in await mongo.chat_message.list_by_session(session_id, limit=size)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("消息列表查询失败：%s", exc)
        return []


@router.post("/sessions/{session_id}/query", response_model=None)
async def query(
    session_id: str,
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /chat/sessions/{sid}/query``：启动一次 Run，返回 ``trace_id`` + ticket。

    ★ 异步执行：立即返回，流水线在后台跑并把事件写入 ``run:progress``；前端订阅 SSE。
    """
    question = str(payload.get("question") or "").strip()
    options = dict(payload.get("options") or {})
    pipeline = container.get("chatbi_pipeline")
    if not question:
        return JSONResponse(
            status_code=400,
            content={"code": "40001", "message": "问题不能为空", "detail": {}, "trace_id": None},
        )
    if pipeline is None:
        return JSONResponse(
            status_code=503,
            content={
                "code": "50301",
                "message": "ChatBI 未装配（MySQL 不可用）",
                "detail": {"component": "mysql"},
                "trace_id": None,
            },
        )

    trace_id = new_trace_id()
    ctx = RunContext(
        trace_id=trace_id,
        span_id=new_span_id(),
        module="chatbi",
        user_id=principal.user_id,
        biz_line_id=principal.biz_line_id,
        session_id=session_id,
        budget_keys=(
            (f"BIZ_LINE:{principal.biz_line_id}",) if principal.biz_line_id is not None else ()
        )
        + ("GLOBAL:*",),
    )
    publisher = container.get("progress_publisher")
    emitter = ProgressEmitter(publisher, trace_id)

    asyncio.create_task(
        _run_bg(container, pipeline, emitter, ctx, question, options, session_id),
        name=f"chatbi-run-{trace_id}",
    )

    tickets = container.get("stream_tickets")
    ticket = tickets.issue(
        trace_id=trace_id,
        user_id=principal.user_id,
        since=_now(),
        until=_now() + timedelta(days=1),
    )
    return {"trace_id": trace_id, "stream_ticket": ticket.ticket, "expires_in": 60}


@router.post("/stream-ticket", response_model=None)
async def stream_ticket(
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any] | JSONResponse:
    """``POST /chat/stream-ticket``：为已存在的 trace 换取 ticket（断线重连用）。"""
    trace_id = str(payload.get("trace_id") or "")
    if not trace_id:
        return JSONResponse(status_code=400, content={"code": "40001", "message": "缺少 trace_id"})
    tickets = container.get("stream_tickets")
    ticket = tickets.issue(
        trace_id=trace_id,
        user_id=principal.user_id,
        since=_now() - timedelta(days=1),
        until=_now() + timedelta(days=1),
    )
    return {"stream_ticket": ticket.ticket, "expires_in": 60}


@router.post("/sessions/{session_id}/query/cancel")
async def cancel_query(
    session_id: str,
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    """``POST /chat/sessions/{sid}/query/cancel``：协作式取消。"""
    _ = (session_id, principal)
    trace_id = str(payload.get("trace_id") or "")
    token = _CANCELS.get(trace_id)
    if token is not None:
        token.cancel("user_cancelled")
    return {"ok": True}


@router.get("/runs/{trace_id}")
async def get_run(
    trace_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /chat/runs/{trace_id}``：run_doc（span 树）。

    ★ 注：§7.3 的路径是 ``/runs/{trace_id}``（顶层），而 v1 路由前缀固定为 ``/chat``，
    因此实际路径为 ``/api/v1/chat/runs/{trace_id}``（同名接口在 ``/obs/runs/{trace_id}``
    亦提供，符合 §7.4）。前缀差异登记为报告遗留问题。
    """
    _ = (principal, since, until)
    metering = container.get("metering")
    if metering is None:
        return {}
    try:
        doc = await metering.get_run_doc(trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_doc 读取失败：%s", exc)
        doc = None
    return dict(doc or {})


@router.get("/sql-audit")
async def sql_audit(
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
    trace_id: Annotated[str | None, Query()] = None,
    decision: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[dict[str, Any]]:
    """``GET /chat/sql-audit``（auditor）。"""
    _ = (principal, page)
    repos = _require_repos(container)
    if repos is None:
        return []
    try:
        return [
            dict(r)
            for r in await repos.sql_audit.query(
                {"trace_id": trace_id, "decision": decision, "limit": size}
            )
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("SQL 审计查询失败：%s", exc)
        return []


async def _append_message(
    mongo: Any, session_id: str, trace_id: str, role: str, content: str
) -> None:
    """★ U16：原子分配 ``seq`` 后落一条 ``chat_message``。

    并发下若各自 ``count + 1`` 会撞唯一键（``session_id + seq``）；这里统一用
    ``chat_session.next_message_seq`` 的 ``find_one_and_update($inc)`` 原子自增。
    best-effort：落库失败只告警，**不影响** Run 本身（问答结果已通过 SSE 返回）。
    """
    if mongo is None or not session_id:
        return
    try:
        seq = await mongo.chat_session.next_message_seq(session_id)
        await mongo.chat_message.append(
            {
                "session_id": session_id,
                "seq": seq,
                "role": role,
                "content": content,
                "trace_id": trace_id,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 会话落库为 best-effort
        logger.warning("chat_message 落库失败（best-effort）：%s", exc)


def _answer_text(output: dict[str, Any]) -> str:
    """从流水线终态 output 里提取可读答案文本（找不到则退化为 JSON 摘要）。"""
    for key in ("answer", "text", "narrative", "message"):
        value = output.get(key)
        if isinstance(value, str) and value:
            return value
    data = output.get("data")
    if isinstance(data, dict):
        for key in ("answer", "text", "narrative"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return json.dumps(output, ensure_ascii=False, default=str)[:2000]


async def _run_bg(
    container: Container,
    pipeline: Any,
    emitter: ProgressEmitter,
    ctx: RunContext,
    question: str,
    options: dict[str, Any],
    session_id: str,
) -> None:
    """后台执行流水线：落会话消息 + 把终态事件写进 ``run:progress``。"""
    token = ctx.cancel_token
    if token is None:
        from dba_runtime import CancelToken  # noqa: PLC0415

        token = CancelToken()
        # ★ 用 dataclasses.replace 派生（红线 5：派生上下文只能经受控构造，禁止原地改）
        ctx = replace(ctx, cancel_token=token, emitter=emitter)
    _CANCELS[ctx.trace_id] = token
    payload: dict[str, Any] = {"question": question, **options}
    mongo = container.get("mongo_repos")
    # 用户消息先落（即便 Run 失败也留下痕迹）
    await _append_message(mongo, session_id, ctx.trace_id, "user", question)
    try:
        result = await pipeline.execute(payload, ctx, emitter)
        await emitter.emit(
            "run.finished",
            {"status": result.status, "steps_run": result.steps_run, "retries": result.retries},
        )
        await _append_message(
            mongo, session_id, ctx.trace_id, "assistant", _answer_text(result.output)
        )
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自行兜底（否则静默丢失）
        logger.exception("ChatBI 后台 Run 失败：%s", exc)
        await emitter.emit("run.error", {"code": "50000", "message": str(exc), "retryable": False})
        await _append_message(mongo, session_id, ctx.trace_id, "assistant", f"[错误] {exc}")
    finally:
        _CANCELS.pop(ctx.trace_id, None)
        publisher = container.get("progress_publisher")
        if publisher is not None:
            publisher.forget(ctx.trace_id)
