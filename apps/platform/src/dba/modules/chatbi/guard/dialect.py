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

from .ast_utils import parse_one_or_deny
from .base import GuardResult, sql_guard_error

__all__ = ["DialectGuard"]


class DialectGuard:
    """方言往返校验（``parse_one`` → 渲染 → ``transpile``）。"""

    name = "dialect"

    def __init__(self, dialect: str = "mysql") -> None:
        self.dialect = dialect

    async def check(self, sql: str, ctx: RunContext, scope: object | None) -> GuardResult:
        """② 道：确认 SQL 能在目标方言下「解析 → 渲染 → 再解析」。

        输入：待校验 SQL、上下文、权限子域（本护栏不消费）。
        输出：``GuardResult(ok=True)``（**不改写 SQL**，往返只作校验）。
        注意：本护栏只回答「目标方言能否表达」，**不判断函数是否白名单**
              （那是 ① 只读护栏的职责，见模块 docstring 的职责拆分说明）。
        """
        _ = (ctx, scope)
        # 解析失败统一转 SQL_PARSE_ERROR（复用共享解析入口，避免各护栏各写一遍）
        tree = parse_one_or_deny(sql, dialect=self.dialect, stage=self.name)
        try:
            rendered = tree.sql(dialect=self.dialect)
            roundtrip = sqlglot.transpile(rendered, read=self.dialect, write=self.dialect)
        except Exception as exc:  # noqa: BLE001
            raise sql_guard_error(
                f"方言渲染失败：{exc}", "SQL_DIALECT_UNSUPPORTED", stage=self.name
            ) from exc
        if not roundtrip or not roundtrip[0].strip():
            raise sql_guard_error(
                "SQL 无法在目标方言下表达", "SQL_DIALECT_UNSUPPORTED", stage=self.name
            )
        return GuardResult(ok=True)
