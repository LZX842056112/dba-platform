"""SQL AST 公共工具（护栏链共享，**单一来源**）。

为什么需要这个模块（消除重复）
------------------------------
护栏链里 ``readonly``（① 只读）与 ``row_scope``（③ 行级注入 / ③' 覆盖断言）都要做
「从 AST 里取出物理表名 / 别名映射」。此前这段逻辑在**两处各写了一份**：

* ``readonly.py`` 里有 ``_tables_in``（排除 CTE 别名）；
* ``row_scope.py`` 里有 ``_tables_in`` / ``_alias_map`` / ``_column_usage``。

两份实现一旦漂移就会出现「① 判定允许的表，③ 判定的却是另一张表」这类**安全不对称**
——例如某天给 CTE 排除逻辑补了一个边界条件，却只改了其中一份。

因此把「与具体护栏无关」的 AST 取数工具统一收口到本模块，两个护栏都从这里 import。

约定
----
* 所有函数都是**纯函数**（不改 AST、无副作用），入参 ``tree`` 为 sqlglot 解析树节点；
* 返回的表名一律是**原始大小写**（调用方自行 ``.lower()`` 归一）；
* 本模块不做任何「安全判定」，只提供客观事实（有哪些表、别名是什么）。
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from .base import sql_guard_error

__all__ = [
    "cte_names",
    "physical_tables",
    "alias_map",
    "column_usage",
    "parse_one_or_deny",
]


def cte_names(tree: Any) -> set[str]:
    """取 AST 中所有 CTE 的名字。

    ★ 必须排除 CTE 名：``WITH t AS (SELECT ...) SELECT * FROM t`` 里 ``t`` 是查询内
    定义的临时结果集，**不是物理表**。若不排除，表级白名单会把 CTE 名当未知物理表而误拒。
    """
    return {c.alias_or_name for c in tree.find_all(exp.CTE)}


def physical_tables(tree: Any) -> set[str]:
    """取 AST 中出现的**物理表**名集合（已排除 CTE 别名）。

    输入：sqlglot 解析树（``exp.Expression`` 任意节点）。
    输出：表名字符串集合；无表返回空集。
    注意：同一个表被 join 多次只返回一次（集合去重）。
    """
    excluded = cte_names(tree)
    return {t.name for t in tree.find_all(exp.Table) if t.name and t.name not in excluded}


def alias_map(tree: Any) -> dict[str, str]:
    """构造 ``别名 / 表名 → 物理表名`` 的映射。

    用途：把 ``d.region`` 这样的限定列名解析回它**真正所属的物理表**。
    输入：sqlglot 解析树；输出：``{别名或表名: 物理表名}``。
    注意：同名后出现者覆盖前者（sqlglot 树顺序遍历）；无别名时映射为自身。
    """
    mapping: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        if not table.name:
            continue
        mapping[table.name] = table.name
        alias = table.alias
        if alias:
            mapping[alias] = table.name
    return mapping


def column_usage(tree: Any, column: str) -> tuple[bool, set[str]]:
    """统计名为 ``column`` 的列在 AST 中如何被引用。

    返回 ``(是否存在无限定引用, 被显式限定的所有者物理表集合)``。

    ★ 这是行级权限「表感知」判定的基础（见 ``row_scope._scope_target_table``）：
      只问「列名是否出现过」会被同名跨表误导——
        * 权限列在**维表**上（``d.region``）却被判成主表列 → 挂到不存在的列 → 误拒；
        * 或恰好挂到主表同名但语义不同的列 → **真实越权泄露**。
      按 ``exp.Column.table`` 把引用解析回真实所属表，才能区分二者。

    输入：sqlglot 解析树 + 目标列名。
    输出：``(unqualified, owners)``——``unqualified=True`` 表示存在 ``region`` 这种
    不带表限定的引用；``owners`` 是显式限定过该列的物理表名集合。
    """
    aliases = alias_map(tree)
    unqualified = False
    owners: set[str] = set()
    for col in tree.find_all(exp.Column):
        if col.name != column:
            continue
        qualifier = col.table
        if not qualifier:
            unqualified = True
            continue
        owners.add(aliases.get(qualifier, qualifier))
    return unqualified, owners


def parse_one_or_deny(sql: str, *, dialect: str = "mysql", stage: str) -> Any:
    """把 SQL 解析为**单棵** AST；语法错误统一转成不可回退的 ``SQL_PARSE_ERROR``。

    输入：``sql`` 原文、``dialect``（默认 mysql）、``stage``（哪道护栏，写入审计）。
    输出：sqlglot 解析树根节点。
    注意：``stage`` 必填——审计要能定位是哪道护栏解析失败的；
          抛出的 ``SqlGuardError`` 由 ``Pipeline._classify`` 归类并按步策略处理。
    """
    try:
        return sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:  # noqa: BLE001 - sqlglot 抛出的异常族不稳定，统一收口
        raise sql_guard_error(f"语法解析失败：{exc}", "SQL_PARSE_ERROR", stage=stage) from exc
