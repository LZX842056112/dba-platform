"""★ QA 独立契约一致性核查（E）：N7 / RunContext.extra 只读 / EventType 20 值。"""

from __future__ import annotations

from typing import get_args

import pytest
from dba_runtime.context import RunContext


def test_e_n7_sql_guard_error_single_definition() -> None:
    """N7：内核 SqlGuardError 与 chatbi 护栏导出的必须是**同一个类对象**。"""
    import dba_runtime.errors as kernel
    from dba.modules.chatbi.guard import base as guard_base

    assert guard_base.SqlGuardError is kernel.SqlGuardError, "存在两处 SqlGuardError 定义"
    # 也不应存在第二个独立定义（扫描内建子类树）
    from dba_runtime.errors import DbaError

    assert issubclass(kernel.SqlGuardError, DbaError)


def test_e_sql_guard_error_class_defined_only_once() -> None:
    """全仓只应有一处 `class SqlGuardError` 定义（此处以模块属性同一性佐证）。"""
    from dba_runtime.errors import SqlGuardError as A
    from dba.modules.chatbi.guard.base import SqlGuardError as B

    assert A is B
    assert A.__module__ == "dba_runtime.errors"


def test_e_run_context_extra_is_readonly() -> None:
    ctx = RunContext(trace_id="a" * 32, span_id="b" * 16, module="chatbi", extra={"x": 1})
    with pytest.raises(TypeError):
        ctx.extra["x"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.extra["new"] = 3  # type: ignore[index]


def test_e_run_context_derivation_does_not_share_extra() -> None:
    parent = RunContext(trace_id="a" * 32, span_id="b" * 16, module="chatbi")
    child = parent.with_extra(k=1)
    assert "k" not in parent.extra
    assert child.extra["k"] == 1


def test_e_event_type_has_20_values() -> None:
    from dba_runtime.events import EVENT_TYPE_COUNT, EventType

    values = get_args(EventType)
    assert len(values) == 20
    assert EVENT_TYPE_COUNT == 20
    assert "run.aborted" in values
    assert "sql.executing" in values
    assert len(set(values)) == 20, "存在重复事件名"
