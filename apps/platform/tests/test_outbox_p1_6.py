"""★ DoD: P1-6 outbox 崩溃不丢数（真实 MySQL）+ P1-9 失败落 DLQ。

「kill -9 不丢数」的本质是**持久化**：记录必须先落 ``outbox`` 表（而非进程内队列）。
本测试用「换一个全新引擎/连接重新读」来模拟进程重启——若数据只在内存，读回必为 0。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from dba.capabilities.telemetry import LocalDlqWriter, OutboxDispatcher, OutboxMeteringService
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.repo import OutboxRepo

pytestmark = pytest.mark.usefixtures("mysql_dsn")


async def test_outbox_survives_process_restart_and_dlq(mysql_dsn: str, tmp_path: Path) -> None:
    engine = build_engine(mysql_dsn)
    try:
        repo = OutboxRepo(engine.rw_engine) if hasattr(engine, "rw_engine") else OutboxRepo(engine)
        # 用带随机后缀的 payload 以便只统计本次写入
        marker = uuid.uuid4().hex
        service = OutboxMeteringService(repo)
        await service.record_llm({"trace_id": marker, "model": "gpt-4o", "status": "ok"})
        await service.record_tool({"trace_id": marker, "tool_name": "sql_exec", "status": "ok"})
        await service.record_span({"trace_id": marker, "span_id": "s1", "status": "ok"})

        pending_before = await repo.pending_count()
        assert pending_before >= 3
    finally:
        await engine.dispose()

    # ── 模拟「进程被 kill -9 后重启」：全新引擎重新读 ──────────────
    engine2 = build_engine(mysql_dsn)
    try:
        repo2 = OutboxRepo(engine2)
        pending_after = await repo2.pending_count()
        assert pending_after == pending_before, "进程重启后 outbox 记录丢失 → P1-6 未满足"
        assert pending_after >= 3
    finally:
        await engine2.dispose()

    # ── 投递：一个必然失败的 handler → 达阈值落 DLQ ────────────────
    dlq = LocalDlqWriter(tmp_path / "dlq.jsonl")
    engine3 = build_engine(mysql_dsn)
    try:
        repo3 = OutboxRepo(engine3)

        async def always_fail(_payload: dict) -> None:
            raise RuntimeError("boom")

        dispatcher = OutboxDispatcher(
            repo3,
            {"llm": always_fail, "tool": always_fail, "span": always_fail},
            dlq=dlq,
            max_attempts=2,
        )
        await dispatcher.run_once()  # attempts 0→1，状态回 pending（重试）
        await dispatcher.run_once()  # attempts 1→2 达阈值 → mark_failed + DLQ
        assert dlq.replay(), "投递失败未落 DLQ（P1-9）"
        assert dlq.dropped_total >= 1

        # ── 新入队的正常记录可被成功投递 ──────────────────────────
        await repo3.enqueue(
            [
                {
                    "kind": "llm",
                    "payload": {"trace_id": "ok" + marker, "status": "ok"},
                    "status": "pending",
                },
                {
                    "kind": "tool",
                    "payload": {"trace_id": "ok" + marker, "status": "ok"},
                    "status": "pending",
                },
            ]
        )

        async def ok(_payload: dict) -> None:
            return None

        ok_dispatcher = OutboxDispatcher(
            repo3, {"llm": ok, "tool": ok, "span": ok}, dlq=dlq, max_attempts=5
        )
        await ok_dispatcher.run_once()
        assert ok_dispatcher.dispatched_total >= 2
    finally:
        await engine3.dispose()


async def test_outbox_enqueue_failure_falls_to_dlq(tmp_path: Path) -> None:
    """outbox 都写不进去（极端故障）时，必须落 DLQ，绝不静默丢（P1-9）。"""
    dlq = LocalDlqWriter(tmp_path / "dlq2.jsonl")

    class BrokenOutbox:
        async def enqueue(self, rows: list[dict]) -> None:
            raise RuntimeError("outbox unavailable")

    service = OutboxMeteringService(BrokenOutbox(), dlq=dlq)
    await service.record_llm({"trace_id": "x", "status": "ok"})  # type: ignore[typeddict-item]
    assert dlq.dropped_total == 1
    assert dlq.replay()[0].kind == "llm"
