"""SQL 安全护栏子包（ChatBI 安全红线）。

护栏链**顺序**（§6.3.2，五道；executor 与 pipeline 都只认这个顺序）::

    readonly → dialect → row_scope → row_scope_verify → limit → dry_run

* ``readonly``         ① 只读白名单 + 单语句 + 危险构造/函数黑名单 + 表/函数白名单
* ``dialect``          ② 目标方言往返校验
* ``row_scope``        ③ **权威防线**：AST 注入行级权限谓词
* ``row_scope_verify`` ③' 兜底：顶层 AND 链合取项断言
* ``limit``            ④ 行数上限 + ``MAX_EXECUTION_TIME`` 提示
* ``dry_run``          ⑤ 只读账号 ``EXPLAIN`` 预执行
"""

from __future__ import annotations

from .base import GuardResult, SqlGuard, SqlGuardChain, SqlGuardError, sql_guard_error
from .dialect import DialectGuard
from .dry_run import DryRunGuard, ReadOnlyConnection, ReadOnlyPermissionError
from .limit_guard import LimitGuard
from .readonly import DEFAULT_FUNCTION_WHITELIST, ReadonlyGuard
from .row_scope import (
    CompiledScope,
    FieldMappingJoinResolver,
    JoinPath,
    JoinPathResolver,
    RowScopeCompiler,
    RowScopeInjector,
    RowScopeVerifier,
    ScopePredicate,
)

__all__ = [
    "GuardResult",
    "SqlGuard",
    "SqlGuardChain",
    "SqlGuardError",
    "sql_guard_error",
    "ReadonlyGuard",
    "DEFAULT_FUNCTION_WHITELIST",
    "DialectGuard",
    "RowScopeCompiler",
    "RowScopeInjector",
    "RowScopeVerifier",
    "ScopePredicate",
    "CompiledScope",
    "JoinPath",
    "JoinPathResolver",
    "FieldMappingJoinResolver",
    "LimitGuard",
    "DryRunGuard",
    "ReadOnlyConnection",
    "ReadOnlyPermissionError",
    "build_guard_chain",
]


def build_guard_chain(
    *,
    audit: object,
    scope_rule_repo: object,
    field_mapping_repo: object,
    readonly_conn: ReadOnlyConnection,
    allowed_tables: set[str] | None = None,
    extra_functions: set[str] | None = None,
    table_loader: object | None = None,
    max_rows: int = 5000,
    timeout_s: float = 30.0,
) -> SqlGuardChain:
    """按冻结顺序装配五道护栏（+ 兜底断言）。"""
    compiler = RowScopeCompiler(scope_rule_repo)
    resolver = FieldMappingJoinResolver(field_mapping_repo)
    guards: list[SqlGuard] = [
        ReadonlyGuard(
            allowed_tables=allowed_tables,
            extra_functions=extra_functions,
            table_loader=table_loader,  # type: ignore[arg-type]
        ),
        DialectGuard(),
        RowScopeInjector(compiler, resolver),
        RowScopeVerifier(resolver),
        LimitGuard(max_rows=max_rows, timeout_s=timeout_s),
        DryRunGuard(readonly_conn),
    ]
    return SqlGuardChain(guards, audit)  # type: ignore[arg-type]
