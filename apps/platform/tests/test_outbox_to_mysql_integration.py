"""★ 集成：内核埋点 → outbox → MySQL 明细（防「字段不匹配」被静默丢）。

内核 ``LLMCallRecord`` 里含 ``parent_span_id`` 等**非 llm_call 表列**的字段
（span 树字段在 Mongo ``run_doc``）。若不过滤就 INSERT，会因未知列失败 → 埋点落 DLQ →
成本明细静默缺失。本测试直接验证 ``_build_outbox_handlers`` 的裁剪与落库。
"""

from __future__ import annotations

import uuid

import pytest
from dba.di import StorageBundle, _build_outbox_handlers
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.repo import MysqlRepositories, OutboxRepo

pytestmark = pytest.mark.usefixtures("mysql_dsn")


async def test_llm_record_with_extra_span_fields_lands_in_llm_call(mysql_dsn: str) -> None:
    engine = build_engine(mysql_dsn)
    try:
        repos = MysqlRepositories(engine)
        bundle = StorageBundle(mysql=engine, repos=repos, available={"mysql": True})
        handlers = _build_outbox_handlers(bundle)
        assert "llm" in handlers and "tool" in handlers

        outbox = OutboxRepo(engine)
        trace_id = uuid.uuid4().hex[:32]
        # 模拟内核 record：含 parent_span_id（非 llm_call 列）
        await outbox.enqueue(
            [
                {
                    "kind": "llm",
                    "status": "pending",
                    "payload": {
                        "trace_id": trace_id,
                        "span_id": "a" * 16,
                        "parent_span_id": "b" * 16,  # ← 必须被过滤掉
                        "provider": "openai",
                        "model": "gpt-4o",
                        "prompt_tokens": 1000,
                        "cached_tokens": 600,
                        "completion_tokens": 200,
                        "cost_micro_usd": 1750,
                        "latency_ms": 123,
                        "status": "ok",
                        "usage_source": "measured",
                        "error_code": None,
                    },
                }
            ]
        )
        from dba.capabilities.telemetry import OutboxDispatcher

        dispatcher = OutboxDispatcher(outbox, handlers, max_attempts=1)
        result = await dispatcher.run_once()
        assert result["claimed"] >= 1
        assert result["done"] >= 1, "埋点投递失败（字段未裁剪？）"

        # 直接查库确认落到了 llm_call
        import sqlalchemy as sa

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT prompt_tokens, cached_tokens, cost_micro_usd, status "
                        "FROM llm_call WHERE trace_id = :t"
                    ),
                    {"t": trace_id},
                )
            ).fetchone()
        assert row is not None, "llm_call 未落库"
        assert int(row[0]) == 1000 and int(row[1]) == 600
        assert int(row[2]) == 1750 and row[3] == "ok"
    finally:
        await engine.dispose()
