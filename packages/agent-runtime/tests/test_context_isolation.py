"""★ P0-9：RunContext 不可变与分支隔离（验证 v1 的跨分支污染已修复）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from dba_runtime import RunContext


def _root(**kw: object) -> RunContext:
    return RunContext(trace_id="a" * 32, span_id="s0", module="system", **kw)  # type: ignore[arg-type]


def test_extra_is_readonly_mapping() -> None:
    parent = _root(extra={"a": 1})
    # ★ v2 / P0-9：extra 是只读映射，原地修改直接 TypeError
    with pytest.raises(TypeError):
        parent.extra["b"] = 2  # type: ignore[index]


def test_child_extra_does_not_mutate_parent() -> None:
    parent = _root(extra={"a": 1})
    child = parent.child("nested", "s1")
    # 子上下文用「复制出的新 dict」包装，父上下文不受影响（v1 会共享同一 dict）
    assert dict(child.extra) == {"a": 1, "span_name": "nested"}
    assert dict(parent.extra) == {"a": 1}
    assert child.span_id == "s1"
    assert child.trace_id == parent.trace_id  # 同一次 Run
    with pytest.raises(TypeError):
        child.extra["z"] = 9  # type: ignore[index]


def test_with_extra_is_only_entry_and_parent_unchanged() -> None:
    parent = _root()
    child = parent.with_extra(k=1)
    assert "k" not in parent.extra
    assert child.extra["k"] == 1


def test_sibling_downgrade_count_independent() -> None:
    # ★ U10 / P0-9：降级计数是显式字段，兄弟分支互相独立
    root = _root()
    assert root.downgrade_count == 0

    branch_a = root.with_downgrade("eco", root.quality)
    branch_b = root.with_downgrade("max", root.quality)

    assert root.downgrade_count == 0  # 父不变
    assert branch_a.downgrade_count == 1
    assert branch_b.downgrade_count == 1  # 兄弟互不影响（v1 会因共享 extra 而互相污染）
    assert branch_a.quality == "eco"
    assert branch_b.quality == "max"
    assert branch_a.downgraded_from == "std"
    assert branch_b.downgraded_from == "std"

    deeper = branch_a.with_downgrade("eco", branch_a.quality)
    assert deeper.downgrade_count == 2
    assert branch_a.downgrade_count == 1  # 再派生仍不污染原分支


def test_frozen_dataclass_blocks_attribute_reassign() -> None:
    ctx = _root()
    with pytest.raises(FrozenInstanceError):
        ctx.quality = "max"  # type: ignore[misc]


def test_memory_hit_rate() -> None:
    ctx = _root(memory_hits=("a", "b"), memory_lookups=4)
    assert ctx.memory_hit_rate == 0.5
    assert _root().memory_hit_rate == 0.0
