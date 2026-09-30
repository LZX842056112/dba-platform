"""``scope_hash`` 构造（★ §6.6）。

对齐《设计文档 v2》§6.6 与《实现要点清单》§6.6、P0-10。

★ 为什么必须把 ``rule_version`` 纳入哈希（照抄 v1 会怎样错）
----------------------------------------------------------
v1 的 ``scope_hash`` = hash(可访问表 + 谓词)。问题是：管理员**改了行级权限规则**
（如把某角色的可见区域从「华东」改成「全国」）之后，可访问表与谓词字符串可能**没变**
（变的只是规则里的取值），于是 ``scope_hash`` 不变 →
语义缓存继续命中**旧权限范围**的答案 → **越权/漏权**。

v2 修法：把该角色集 ``row_scope_rule`` 的**最大 updated_at**（``rule_version``）
一并纳入哈希。规则一改 → 版本变 → hash 变 → 旧缓存自然失效。

定义：``scope_hash = sha256(canonical_json({tables, predicates, rule_version}))[:32]``
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ScopeFingerprint",
    "RULE_VERSION_ABSENT",
    "build_scope_hash",
    "canonical_json",
    "empty_scope_hash",
]


#: ``rule_version`` 缺失时的稳定占位（与「有规则但值为空」区分开）
RULE_VERSION_ABSENT = "__none__"


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空白、非 ASCII 直出（保证跨进程字节一致）。

    ★ 必须**规范化**：同一份权限如果字典插入顺序不同，序列化字节就不同，
    哈希随之不同 → 缓存永远命中不了（或错误命中）。
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ScopeFingerprint:
    """参与 ``scope_hash`` 的三要素。"""

    accessible_tables: tuple[str, ...]
    predicates: tuple[str, ...]
    rule_version: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "tables": sorted(self.accessible_tables),
            "predicates": sorted(self.predicates),
            "rule_version": self.rule_version or RULE_VERSION_ABSENT,
        }


def build_scope_hash(
    accessible_tables: list[str] | tuple[str, ...],
    predicates: list[str] | tuple[str, ...],
    rule_version: str | None = None,
) -> str:
    """构造 ``scope_hash``（32 位十六进制）。

    :param accessible_tables: 该用户可访问的物理表名集合
    :param predicates: 行级谓词的**规范文本**集合（如 ``"region IN ('east')"``）
    :param rule_version: 该角色集 ``row_scope_rule`` 的最大 ``updated_at``（ISO8601）
    """
    fingerprint = ScopeFingerprint(
        accessible_tables=tuple(accessible_tables),
        predicates=tuple(predicates),
        rule_version=rule_version,
    )
    digest = hashlib.sha256(canonical_json(fingerprint.as_payload()).encode("utf-8")).hexdigest()
    return digest[:32]


def empty_scope_hash() -> str:
    """空权限范围的哈希（用于「一行都不该看见」的判定基线）。"""
    return build_scope_hash((), (), None)
