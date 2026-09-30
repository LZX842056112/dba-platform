"""② 方言护栏：确保生成的 SQL 在目标方言上真的可执行。

对齐《设计方案 v2》§6.3.2 ② 与《实现要点清单》§5.8。

★ v2 的修法（照抄 v1 会怎样错）
------------------------------
v1 用「是否存在 ``sqlglot.exp.Anonymous`` 节点」判断未知函数——这个判据**既会漏**
（sqlglot 认识但 MySQL 不支持的函数）**也会误杀**（新增的合法函数）。
v2 把它拆成「函数白名单（① 只读护栏）+ 方言往返校验（本护栏）」：
本护栏只负责「目标方言能否表达」这件事。
"""

from __future__ import annotations

import sqlglot
from dba_runtime.context import RunContext

from .base import GuardResult, sql_guard_error

__all__ = ["DialectGuard"]


class DialectGuard:
    """方言往返校验（``parse_one`` → 渲染 → ``transpile``）。"""

    name = "dialect"

    def __init__(self, dialect: str = "mysql") -> None:
        self.dialect = dialect

    async def check(self, sql: str, ctx: RunContext, scope: object | None) -> GuardResult:
        _ = (ctx, scope)
        try:
            tree = sqlglot.parse_one(sql, dialect=self.dialect)
            rendered = tree.sql(dialect=self.dialect)
            roundtrip = sqlglot.transpile(rendered, read=self.dialect, write=self.dialect)
        except Exception as exc:  # noqa: BLE001
            raise sql_guard_error(
                f"方言校验失败：{exc}", "SQL_PARSE_ERROR", stage=self.name
            ) from exc
        if not roundtrip or not roundtrip[0].strip():
            raise sql_guard_error(
                "SQL 无法在目标方言下表达", "SQL_DIALECT_UNSUPPORTED", stage=self.name
            )
        return GuardResult(ok=True)
