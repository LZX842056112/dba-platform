"""埋点服务与 outbox 投递器（★ P1-6 的核心实现）。

对齐《设计文档 v2》§6.1.4 / §5.6 与《实现要点清单》P1-6 / P1-9。

★ P1-6：**禁止内存队列**（照抄 v1 会怎样错）
------------------------------------------
v1 用进程内 ``asyncio.Queue`` 做「统一投递」：
  1) 进程崩溃 / kill -9 → 队列里未投递的埋点与成本**永久丢失**（账不平）；
  2) 多副本部署时队列是**进程私有**的，无法共享、无法回放；
  3) 投递失败无持久化重试、无 DLQ 对账。

v2 改为 **transactional outbox**：
  * ``record_*``（内核 ``drain_queue`` 调用）→ 同步写 ``outbox`` 表（**持久化**）；
  * 独立投递协程 ``claim()``（``FOR UPDATE SKIP LOCKED``，多副本不重复抢）→ 按 kind 分发
    → 成功 ``mark_done``；失败累加 ``attempts``，超阈值置 ``failed`` 并落 **DLQ 文件** + 计数
    ``telemetry_dropped_total``。
  * kill -9 后未投递记录仍留在表中，重启即可补投 → **不丢数**。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from dba_runtime.telemetry import LLMCallRecord, SpanRecord, ToolCallRecord

from .dlq import LocalDlqWriter

__all__ = ["OutboxMeteringService", "OutboxDispatcher"]

logger = logging.getLogger("dba.capabilities.telemetry")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class OutboxMeteringService:
    """内核 ``MeteringService`` 的持久化实现（写 outbox 表，不直接落目标存储）。

    ★ 与内核 ``bind_metering(service)`` 配合：内核在 ``finally`` 同步入队（不阻塞业务），
    后台 ``drain_queue`` 调用本服务的方法；本服务再写 outbox（持久化），
    由 ``OutboxDispatcher`` 异步投递到 MySQL ``llm_call``/``tool_call``、Mongo ``run_doc``、ES。
    """

    def __init__(self, outbox_repo: Any, *, dlq: LocalDlqWriter | None = None) -> None:
        self._outbox = outbox_repo
        self._dlq = dlq or LocalDlqWriter()

    async def _enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            await self._outbox.enqueue([{"kind": kind, "payload": payload, "status": "pending"}])
        except Exception as exc:  # noqa: BLE001 - outbox 写失败必须落 DLQ（绝不静默丢）
            logger.error("outbox 入队失败，落 DLQ：%s", exc)
            await self._dlq.write(kind, f"enqueue_failed: {exc}", payload)

    async def record_llm(self, rec: LLMCallRecord) -> None:
        await self._enqueue("llm", dict(rec))

    async def record_tool(self, rec: ToolCallRecord) -> None:
        await self._enqueue("tool", dict(rec))

    async def record_span(self, rec: SpanRecord) -> None:
        await self._enqueue("span", dict(rec))

    async def record_run(self, rec: dict[str, Any]) -> None:
        """Run 生命周期（开始/收尾）入队 outbox → 异步写 ``run`` 表（观测模块 03 的源数据）。"""
        await self._enqueue("run", rec)

    async def record_dlq(self, kind: str, rec: dict[str, Any]) -> None:
        await self._dlq.write(kind, "kernel_dlq", rec)

    @property
    def dlq(self) -> LocalDlqWriter:
        return self._dlq


class OutboxDispatcher:
    """outbox 投递器：claim → dispatch → mark_done / mark_failed(+DLQ)。

    ``handlers`` 是 ``kind → 异步处理函数`` 的映射（由 DI 装配真实目标存储），
    便于单测注入假 handler 验证「崩溃后不丢 / 重试 / DLQ」。
    """

    def __init__(
        self,
        outbox_repo: Any,
        handlers: dict[str, Handler],
        *,
        dlq: LocalDlqWriter | None = None,
        max_attempts: int = 5,
        batch_size: int = 200,
    ) -> None:
        self._outbox = outbox_repo
        self._handlers = handlers
        self._dlq = dlq or LocalDlqWriter()
        self._max_attempts = max_attempts
        self._batch = batch_size
        self.dispatched_total = 0
        self.failed_total = 0

    async def run_once(self) -> dict[str, int]:
        """处理一批待投递记录，返回 ``{claimed, done, failed}``。"""
        rows = await self._outbox.claim(limit=self._batch)
        done = 0
        failed = 0
        done_ids: list[int] = []
        for row in rows:
            outbox_id = int(row["id"])
            kind = str(row["kind"])
            payload = dict(row.get("payload") or {})
            attempts = int(row.get("attempts", 0) or 0)
            handler = self._handlers.get(kind)
            if handler is None:
                await self._fail(outbox_id, kind, payload, f"no handler for kind={kind}", attempts)
                failed += 1
                continue
            try:
                await handler(payload)
            except Exception as exc:  # noqa: BLE001 - 投递失败：重试/落 DLQ
                await self._fail(outbox_id, kind, payload, str(exc), attempts)
                failed += 1
                continue
            done_ids.append(outbox_id)
            self.dispatched_total += 1
            done += 1
        # ★ 成功的一批一次 UPDATE（不再逐条 mark_done）
        if done_ids:
            await self._outbox.mark_done(done_ids)
        return {"claimed": len(rows), "done": done, "failed": failed}

    async def _fail(
        self, outbox_id: int, kind: str, payload: dict[str, Any], error: str, attempts: int
    ) -> None:
        """失败处理：未达阈值 → ``mark_retry``（状态回 pending，下轮重投）；
        达阈值 → ``mark_failed``（终态）并落 **DLQ**。"""
        if attempts + 1 >= self._max_attempts:
            await self._outbox.mark_failed(outbox_id, error)
            await self._dlq.write(kind, error, payload)
        else:
            await self._outbox.mark_retry(outbox_id, error)
        self.failed_total += 1

    async def run_forever(self, *, poll_interval: float = 0.5) -> None:
        """常驻投递循环（应用 lifespan / worker 启动时创建任务）。"""
        while True:
            stats = await self.run_once()
            if stats["claimed"] == 0:
                await asyncio.sleep(poll_interval)

    def handlers(self) -> dict[str, Handler]:
        return dict(self._handlers)
