"""埋点与计量装饰器（内核 L2）。

对齐《设计方案 v2》§6.1.4 / 《实现要点清单》§5.4、§6.7、U3、U6、
以及 P0-1 / P0-2 两条硬修法。

★ 为什么这样写（照抄 v2 正文会怎样错）
--------------------------------------
P0-1（span 树成立的前提）：
  ``@traced`` 必须 ``set_current(child)`` 并在 ``finally`` 中 ``reset_current(token)``。
  v1 只生成了 ``child`` 却没 ``set_current``，于是被包裹函数体内 ``ctx()`` 读到的仍是
  **父**上下文——所有嵌套 span 平铺成一层，``run_doc.spans[].parent_span_id`` 无法成立。
  同时 ``start_ms`` 必须用 ``time.time()*1000``（**绝对墙钟**，跨进程可对齐）；
  耗时才用 ``time.perf_counter()``（单调计数，跨进程无意义）。

P0-2（失败 / 超时也必须落一条记录）：
  ``@metered`` 用 try/except/finally 覆盖成功 / 失败 / 超时三种情况；v1 注释写「失败也要
  计量」但代码没有 try/finally，异常直接抛出、什么都没记；且 v1 写入的 record 缺
  ``status`` 字段，而 ``llm_call.status`` 是 NOT NULL——真实落库会直接失败。

U3：``LLMCallRecord`` / ``ToolCallRecord`` / ``SpanRecord`` 在文档中只有名字没有定义，
  此处用 ``TypedDict`` 定义，字段覆盖 §5.4 与 DDL 列（含 ``status`` NOT NULL、``error_code``、
  ``price_book_id``、``usage_source``、``cached_tokens``）。

U6：成本换算统一以 **L4 侧签名** ``normalize(rec: LLMCallRecord) -> NormalizedCost`` 为准。
  ★ 有意收敛：内核把 ``normalize`` 定为**同步**方法。原因是装饰器在 ``finally``（可能处于
  协程取消路径）中绝不能 ``await`` 数据库——那正是 v1 丢 span 的同款反模式。L4 的
  ``CostNormalizer`` 以「预热的价格缓存」提供同步实现，价格未命中时返回
  ``NormalizedCost.unknown()``（不静默按 0 计）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from typing import Any, Literal, Protocol, Required, TypedDict, cast

from pydantic import BaseModel

from .context import RunContext, ctx, reset_current, set_current
from .errors import classify

__all__ = [
    "MeteringService",
    "CostNormalizerProto",
    "NormalizedCost",
    "LLMCallRecord",
    "ToolCallRecord",
    "SpanRecord",
    "bind_metering",
    "enqueue",
    "drain_queue",
    "flush_queue",
    "traced",
    "metered",
    "dropped_total",
    "queue_depth",
    "is_bound",
]

#: 被装饰的异步函数类型与装饰器类型（缩短签名，避免 E501）
AsyncFn = Callable[..., Awaitable[Any]]
AsyncDecorator = Callable[[AsyncFn], AsyncFn]

# ── 记录字段（★ U3：TypedDict，覆盖 §5.4 写入字段与 DDL 列） ─────────────
_LLMStatus = Literal["ok", "error", "timeout"]
_ToolStatus = Literal["ok", "error"]
_UsageSource = Literal["measured", "self_reported"]


class LLMCallRecord(TypedDict, total=False):
    """LLM 调用明细记录（对应 MySQL ``llm_call`` 表）。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    biz_line_id: int | None
    agent_uid: str | None
    provider: str
    model: str
    route_policy: str | None
    downgrade_from: str | None
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    ttft_ms: int | None
    latency_ms: int
    cache_hit: int
    cost_micro_usd: int
    price_book_id: int | None
    usage_source: _UsageSource
    error_code: str | None
    created_at: str
    # ★ NOT NULL：必须给，否则落库失败
    status: Required[_LLMStatus]


class ToolCallRecord(TypedDict, total=False):
    """工具调用明细记录（对应 MySQL ``tool_call`` 表）。"""

    trace_id: str
    span_id: str
    biz_line_id: int | None
    agent_uid: str | None
    tool_name: str
    args_hash: str | None
    latency_ms: int
    cost_micro_usd: int
    created_at: str
    status: Required[_ToolStatus]


class SpanRecord(TypedDict, total=False):
    """Span 记录（对应 Mongo ``run_doc.spans[]``）。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    start_ms: int
    duration_ms: int
    error_type: str | None
    error_message: str | None
    status: Required[Literal["ok", "error", "timeout"]]


class NormalizedCost(BaseModel):
    """归一化后的成本（微美元整数）。★ U8：文档引用未定义，此处补最小定义。"""

    input_micro_usd: int = 0
    cached_micro_usd: int = 0
    output_micro_usd: int = 0
    price_book_id: int | None = None
    unit_size: int = 1
    currency: str = "USD"
    fx_rate_to_usd: float = 1.0
    unknown_price: bool = False

    @property
    def total_micro_usd(self) -> int:
        """总成本（三项之和，整数）。"""
        return self.input_micro_usd + self.cached_micro_usd + self.output_micro_usd

    @classmethod
    def unknown(cls, rec: LLMCallRecord | None = None) -> NormalizedCost:
        """未知价格：显式标记 ``price_book_id=NULL`` + ``unknown_price=True``。

        不能静默按 0 算——那会让成本系统性偏低、预算守卫直接失效（计价覆盖率下降）。
        """
        return cls(price_book_id=None, unknown_price=True)


class MeteringService(Protocol):
    """由平台侧实现，写 MySQL 明细 + 上报 OTel。"""

    async def record_llm(self, rec: LLMCallRecord) -> None: ...
    async def record_tool(self, rec: ToolCallRecord) -> None: ...
    async def record_span(self, rec: SpanRecord) -> None: ...
    async def record_dlq(self, kind: str, rec: dict[str, Any]) -> None: ...


class CostNormalizerProto(Protocol):
    """★ U6：成本换算属于 L4 能力（``capabilities/cost``），装饰器通过注入拿到它。

    以 L4 侧签名 ``normalize(rec)`` 为准（而非 v1 的 ``normalize(usage, at)``）。
    内核视其为薄适配，装饰器内负责构造 record。
    """

    def normalize(self, rec: LLMCallRecord) -> NormalizedCost: ...


_METERING: MeteringService | None = None
_COST: CostNormalizerProto | None = None
_QUEUE: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
_DROPPED = 0  # 导出为 telemetry_dropped_total 指标


def bind_metering(
    service: MeteringService,
    *,
    cost: CostNormalizerProto | None = None,
    queue_size: int = 10_000,
) -> None:
    """★ v2：应用与 worker 的启动钩子里都必须调用本函数。

    未绑定时埋点是**静默 no-op**（v1 就是这个行为，worker 侧极易漏配而无人发现）。
    因此 ``/ready`` 必须暴露 ``metering_bound``，worker 启动须断言已绑定。
    """
    global _METERING, _COST, _QUEUE  # noqa: PLW0603
    _METERING = service
    _COST = cost
    _QUEUE = asyncio.Queue(maxsize=queue_size)


def is_bound() -> bool:
    """埋点是否已装配（供 ``/ready`` 暴露 ``metering_bound``）。"""
    return _METERING is not None and _QUEUE is not None


def queue_depth() -> int:
    """当前待投递队列深度（自监控指标）。"""
    return _QUEUE.qsize() if _QUEUE is not None else 0


def dropped_total() -> int:
    """被丢弃的埋点计数（导出为 ``telemetry_dropped_total``）。"""
    return _DROPPED


def enqueue(kind: str, rec: Mapping[str, Any]) -> None:
    """★ v2：埋点入队是**同步非阻塞**操作。

    这比「await + asyncio.shield」更可靠：同步调用不会被协程取消打断，
    因此超时 / 取消路径上的埋点也不会丢。队列满时丢弃并计数——
    宁可丢埋点，也绝不让埋点拖垮主链路。
    """
    global _DROPPED  # noqa: PLW0603
    if _QUEUE is None:
        return
    try:
        _QUEUE.put_nowait((kind, dict(rec)))
    except asyncio.QueueFull:
        _DROPPED += 1


async def _dispatch(service: MeteringService, kind: str, rec: dict[str, Any]) -> None:
    """把一条队列记录投递给具体 handler（失败落 DLQ）。"""
    try:
        if kind == "span":
            await service.record_span(rec)  # type: ignore[arg-type]
        elif kind == "llm":
            await service.record_llm(rec)  # type: ignore[arg-type]
        else:
            await service.record_tool(rec)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        await service.record_dlq(kind, rec)


async def drain_queue(service: MeteringService) -> None:
    """★ v2：后台投递协程（在 FastAPI lifespan / worker 启动时创建）。

    失败不重试到天荒地老，而是落 DLQ 并对账。
    """
    assert _QUEUE is not None
    while True:
        kind, rec = await _QUEUE.get()
        try:
            await _dispatch(service, kind, rec)
        finally:
            _QUEUE.task_done()


async def flush_queue(service: MeteringService) -> int:
    """把当前队列中已有的记录全部投递一次（**测试辅助**，非生产路径）。

    生产路径用 ``drain_queue`` 常驻消费；本函数让内核单测可以在不启后台协程的情况下
    断言落库内容。返回投递条数。
    """
    if _QUEUE is None:
        return 0
    count = 0
    while not _QUEUE.empty():
        kind, rec = _QUEUE.get_nowait()
        await _dispatch(service, kind, rec)
        count += 1
    return count


def traced(name: str | None = None, kind: str = "agent") -> AsyncDecorator:
    """标记一个 async 方法 / 函数为一个 Span。自动读 ContextVar，不需要传 ctx。

    用法::

        @traced("step.sql_gen", kind="agent")
        async def run(self, payload, ctx): ...
    """

    def decorator(fn: AsyncFn) -> AsyncFn:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            parent = ctx()
            span_name = name or fn.__qualname__
            span_id = uuid.uuid4().hex[:16]
            child = parent.child(span_name, span_id)

            # ★ P0-1 修复 1：必须把子上下文绑进 ContextVar。v1 只是生成了 child 却没有
            #   set_current(child)，于是被包裹函数体内 ctx() 读到的仍是父上下文——
            #   嵌套 span 全部平铺成一层，run_doc 的 span 树不成立。
            token = set_current(child)

            # ★ P0-1 修复 2：start_ms 必须用 time.time()（绝对墙钟）；耗时才用 perf_counter。
            started_wall_ms = int(time.time() * 1000)
            started = time.perf_counter()
            status: Literal["ok", "error", "timeout"] = "ok"
            err: BaseException | None = None
            try:
                return await fn(*args, **kwargs)
            except TimeoutError as exc:
                status, err = "timeout", exc
                raise
            except Exception as exc:  # noqa: BLE001
                status, err = "error", exc
                raise
            finally:
                reset_current(token)  # 先还原上下文，再记账
                # ★ P0-1 修复 3：记账改为**同步入队** —— 不再 await 落库。
                #   v1 在 finally 里 await，一旦协程被取消（超时/用户中断），这次 span
                #   就永久丢了；而且 await 会把 DB 延迟加到业务路径上。
                enqueue(
                    "span",
                    {
                        "trace_id": parent.trace_id,
                        "span_id": span_id,
                        "parent_span_id": parent.span_id,
                        "name": span_name,
                        "kind": kind,
                        "start_ms": started_wall_ms,  # 绝对墙钟（ms）
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "status": status,
                        "error_type": type(err).__name__ if err else None,
                        "error_message": str(err) if err else None,
                    },
                )

        return wrapper

    return decorator


def metered(kind: Literal["llm", "tool", "search"]) -> AsyncDecorator:
    """标记一次计费调用。业务代码只调用 SDK，token / 成本 / 状态由装饰器统一落账。

    ★ P0-2 修复（v1 的三处硬伤）：
      1) v1 注释写「失败也要计量」，代码却没有 try/finally——异常直接抛出，什么都没记。
         v2 用 try/except/finally 保证成功、失败、超时三种情况都落一条记录。
      2) v1 写入的 record 缺 ``status`` 字段，而 ``llm_call.status`` 是 NOT NULL。
      3) v1 声称「成本由装饰器完成」，但装饰器里根本没有成本换算；v2 通过注入的
         ``CostNormalizerProto`` 完成换算，并保留 ``price_book_id`` 以便重算与对账。
    """

    def decorator(fn: AsyncFn) -> AsyncFn:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            c = ctx()
            started = time.perf_counter()
            result: Any = None
            status: Literal["ok", "error", "timeout"] = "ok"
            err: BaseException | None = None
            try:
                result = await fn(*args, **kwargs)
                return result
            except TimeoutError as exc:
                status, err = "timeout", exc
                raise
            except Exception as exc:  # noqa: BLE001
                status, err = "error", exc
                raise
            finally:
                elapsed = int((time.perf_counter() - started) * 1000)
                usage: dict[str, Any] = {}
                if result is not None and hasattr(result, "usage_dict"):
                    usage = dict(result.usage_dict())
                elif err is not None:
                    usage = dict(getattr(err, "partial_usage", {}) or {})
                if kind == "llm":
                    enqueue("llm", _build_llm_record(c, usage, elapsed, status, err, result))
                else:
                    enqueue(
                        "tool",
                        {
                            "trace_id": c.trace_id,
                            "span_id": c.span_id,
                            "biz_line_id": c.biz_line_id,
                            "agent_uid": c.agent_uid,
                            "tool_name": kwargs.get("tool_name") or getattr(fn, "__name__", kind),
                            "status": "ok" if status == "ok" else "error",
                            "latency_ms": elapsed,
                        },
                    )

        return wrapper

    return decorator


def _build_llm_record(
    c: RunContext,
    usage: dict[str, Any],
    elapsed: int,
    status: Literal["ok", "error", "timeout"],
    err: BaseException | None,
    result: Any,
) -> LLMCallRecord:
    """由 usage dict 构造 ``LLMCallRecord``（★ U6：装饰器内构造 record 再交给 L4 normalize）。"""
    rec = cast(
        "LLMCallRecord",
        {
            # usage 展开：model / prompt_tokens / completion_tokens / cached_tokens /
            # ttft_ms / usage_source
            **usage,
            "trace_id": c.trace_id,
            "span_id": c.span_id,
            "biz_line_id": c.biz_line_id,
            "agent_uid": c.agent_uid,
            "provider": usage.get("provider") or getattr(result, "provider", None) or "",
            "latency_ms": elapsed,
            "status": status,  # ★ P0-2：NOT NULL，必须给
            "usage_source": usage.get("usage_source", "measured"),
            "error_code": classify(err)[0] if err is not None else None,
        },
    )
    cost = _COST.normalize(rec) if _COST is not None else NormalizedCost.unknown()
    rec["cost_micro_usd"] = cost.total_micro_usd
    rec["price_book_id"] = cost.price_book_id
    return rec
