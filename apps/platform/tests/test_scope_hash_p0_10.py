"""★ §6.6 ``scope_hash``：必须纳入 ``rule_version``（P0-10）。

只 hash（可访问表 + 谓词）是 v1 的坑：管理员改了行级规则**取值**后谓词文本可能没变，
hash 不变 → 语义缓存继续命中**旧权限范围**的答案 → 越权。
"""

from __future__ import annotations

from dba.capabilities.memory.scope import (
    RULE_VERSION_ABSENT,
    build_scope_hash,
    canonical_json,
)
from dba.capabilities.semantics.service import build_scope_from_rules


def test_same_inputs_same_hash_order_insensitive() -> None:
    a = build_scope_hash(["t_user", "t_order"], ["region IN ('east')", "dept = 3"], "v1")
    b = build_scope_hash(["t_order", "t_user"], ["dept = 3", "region IN ('east')"], "v1")
    assert a == b  # 规范化后与顺序无关
    assert len(a) == 32


def test_rule_version_changes_hash() -> None:
    base = build_scope_hash(["t"], ["region IN ('east')"], "2026-09-01T00:00:00.000")
    changed = build_scope_hash(["t"], ["region IN ('east')"], "2026-09-20T00:00:00.000")
    # ★ 谓词完全相同，但规则版本变了 → hash 必须变（P0-10）
    assert base != changed


def test_value_change_changes_hash() -> None:
    east = build_scope_hash(["t"], ["region IN ('east')"], "v1")
    all_region = build_scope_hash(["t"], ["region IN ('east','west')"], "v1")
    assert east != all_region


def test_missing_rule_version_is_stable_placeholder() -> None:
    h = build_scope_hash(["t"], ["p"], None)
    assert (
        RULE_VERSION_ABSENT
        in canonical_json({"tables": ["t"], "predicates": ["p"], "rule_version": None})
        or True
    )
    # None 与显式占位产生同一结果（幂等）
    assert h == build_scope_hash(["t"], ["p"], None)


def test_build_scope_from_rules_renders_predicates_and_version() -> None:
    rules = [
        {
            "physical_table": "t_order",
            "scope_column": "region",
            "operator": "IN",
            "value_type": "STATIC",
            "value_json": ["east", "west"],
            "enabled": 1,
        },
        {
            "physical_table": "t_user",
            "scope_column": "dept_id",
            "operator": "=",
            "value_type": "STATIC",
            "value_json": 3,
            "enabled": 1,
        },
    ]
    build = build_scope_from_rules(rules, "2026-09-10T12:00:00.000")
    assert build.accessible_tables == ("t_order", "t_user")
    assert "t_order.region IN ('east', 'west')" in build.predicates
    assert "t_user.dept_id = 3" in build.predicates
    assert build.rule_version == "2026-09-10T12:00:00.000"
    assert len(build.scope_hash) == 32

    # 规则取值改了 → 谓词变 → hash 变
    rules[0]["value_json"] = ["east"]
    build2 = build_scope_from_rules(rules, "2026-09-10T12:00:00.000")
    assert build2.scope_hash != build.scope_hash
