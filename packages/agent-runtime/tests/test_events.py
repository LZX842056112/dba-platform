"""★ U4：EventType 冻结为 20 个，且必须含 run.aborted / sql.executing。"""

from __future__ import annotations

from typing import get_args

from dba_runtime import EVENT_TYPE_COUNT, EventEnvelope, EventType


def test_event_type_count_is_20() -> None:
    args = get_args(EventType)
    assert len(args) == 20
    assert len(set(args)) == 20  # 无重复
    assert EVENT_TYPE_COUNT == 20


def test_event_type_contains_v2_gaps() -> None:
    args = set(get_args(EventType))
    assert "run.aborted" in args  # ★ v2 补
    assert "sql.executing" in args  # ★ v2 补


def test_envelope_has_required_fields() -> None:
    env = EventEnvelope.build("run.started", 1, "a" * 32, {"module": "chatbi"})
    assert env.seq == 1
    assert env.trace_id == "a" * 32
    assert env.ts > 1_600_000_000_000
    assert env.data == {"module": "chatbi"}
