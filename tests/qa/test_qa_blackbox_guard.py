"""★ QA 独立黑盒验证（B）：只读护栏逐项喂输入 + 必须改写项。

不依赖工程师的用例；每条 SQL 独立断言，直接喂给护栏看是否被拒/被改写。
"""

from __future__ import annotations

from typing import Any

import pytest
from dba.modules.chatbi.guard import (
    CompiledScope,
    JoinPath,
    LimitGuard,
    ReadonlyGuard,
    RowScopeInjector,
)
from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError

_ALLOWED = {"t_order", "t_fact", "t_dim"}


def make_ctx() -> RunContext:
    return RunContext(trace_id="c" * 32, span_id="d" * 16, module="chatbi")


class _Resolver:
    def __init__(self, path: JoinPath | None) -> None:
        self._path = path

    async def join_path_for(self, table: str) -> JoinPath | None:  # noqa: ARG002
        return self._path


class _Compiler:
    pass


async def _raises_symbol(sql: str, symbol: str) -> SqlGuardError:
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    with pytest.raises(SqlGuardError) as ei:
        await guard.check(sql, make_ctx(), None)
    assert ei.value.symbol == symbol, f"{sql!r}: 期望 {symbol} 实得 {ei.value.symbol}"
    return ei.value


# ── 必须被拒（逐项独立断言）──────────────────────────────────────────────
async def test_reject_multi_statement() -> None:
    await _raises_symbol("SELECT 1; DROP TABLE x", "SQL_MULTI_STATEMENT")


async def test_reject_information_schema() -> None:
    await _raises_symbol("SELECT * FROM information_schema.tables", "SQL_TABLE_NOT_ALLOWED")


async def test_reject_into_outfile() -> None:
    await _raises_symbol("SELECT id FROM t_order INTO OUTFILE '/tmp/x'", "SQL_DANGEROUS_CONSTRUCT")


async def test_reject_into_dumpfile() -> None:
    await _raises_symbol(
        "SELECT id FROM t_order INTO DUMPFILE '/tmp/x'", "SQL_DANGEROUS_CONSTRUCT"
    )


async def test_reject_for_update() -> None:
    await _raises_symbol("SELECT id FROM t_order FOR UPDATE", "SQL_DANGEROUS_CONSTRUCT")


async def test_reject_lock_in_share_mode() -> None:
    await _raises_symbol(
        "SELECT id FROM t_order LOCK IN SHARE MODE", "SQL_DANGEROUS_CONSTRUCT"
    )


async def test_reject_session_var_assignment() -> None:
    await _raises_symbol("SELECT @a := 1", "SQL_DANGEROUS_CONSTRUCT")


async def test_reject_comment_injection() -> None:
    await _raises_symbol("/*!50000 SELECT 1 */", "SQL_DANGEROUS_CONSTRUCT")
    await _raises_symbol("SELECT /*!40001 SQL_NO_CACHE */ id FROM t_order", "SQL_DANGEROUS_CONSTRUCT")


async def test_reject_sleep() -> None:
    await _raises_symbol("SELECT SLEEP(10)", "SQL_DANGEROUS_FUNCTION")


async def test_reject_load_file() -> None:
    await _raises_symbol("SELECT LOAD_FILE('/etc/passwd')", "SQL_DANGEROUS_FUNCTION")


async def test_reject_call_proc() -> None:
    await _raises_symbol("CALL some_proc()", "SQL_NOT_READONLY")


async def test_reject_mysql_user() -> None:
    await _raises_symbol("SELECT * FROM mysql.user", "SQL_TABLE_NOT_ALLOWED")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT BENCHMARK(1000000, MD5('x'))",
        "SELECT GET_LOCK('x', 10)",
        "SELECT RELEASE_LOCK('x')",
    ],
)
async def test_reject_more_dangerous_functions(sql: str) -> None:
    await _raises_symbol(sql, "SQL_DANGEROUS_FUNCTION")


# ── 必须被改写 ───────────────────────────────────────────────────────────
async def test_no_limit_gets_limit_added() -> None:
    res = await LimitGuard(max_rows=5000, timeout_s=30).check(
        "SELECT id FROM t_order", make_ctx(), None
    )
    assert res.rewritten_sql is not None
    assert "LIMIT 5000" in res.rewritten_sql
    assert "MAX_EXECUTION_TIME(30000)" in res.rewritten_sql


async def test_over_limit_compressed() -> None:
    res = await LimitGuard(max_rows=5000, timeout_s=30).check(
        "SELECT id FROM t_order LIMIT 999999", make_ctx(), None
    )
    assert res.rewritten_sql is not None
    assert "LIMIT 5000" in res.rewritten_sql
    assert "999999" not in res.rewritten_sql


async def test_dim_table_join_injection_position() -> None:
    """权限列在维表：谓词必须注入到引用事实表的 SELECT 的 WHERE（且用维表限定名）。"""
    inj = RowScopeInjector(
        _Compiler(),  # type: ignore[arg-type]
        _Resolver(JoinPath(dim_table="t_dim", on="t_fact.dim_id = t_dim.id")),  # type: ignore[arg-type]
    )
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = "SELECT t_fact.id FROM t_fact JOIN t_dim ON t_fact.dim_id = t_dim.id"
    res = await inj.check(sql, make_ctx(), scope)
    assert res.scope_injected is True
    assert res.rewritten_sql is not None
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(res.rewritten_sql, dialect="mysql")
    where = tree.args.get("where")
    assert where is not None, "谓词应注入到顶层 WHERE"
    cols = {c.sql(dialect="mysql") for c in where.find_all(exp.Column)}
    assert "t_dim.region" in cols, f"应在 t_dim.region 上注入，实得 {cols}"


async def _unused(_x: Any) -> None:  # pragma: no cover
    return None
