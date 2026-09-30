"""★ B4 端到端（真实 MySQL）：五道护栏 + ``QueryExecutor`` 唯一执行入口。

需要 ``DBA_TEST_MYSQL_DSN`` 且库已 ``dba migrate``（未设置则整文件 skip）；本机私有实例
在 3307。验证「护栏 + 执行 + 审计」在真实数据库上的完整闭环：
  * 行级权限注入**真的落到 SQL**（结果只含权限值域内的行）；
  * 审计落 ``sql_audit``（rewrite / deny 两类 + ``scope_injected`` / ``scope_hash``）；
  * 超 max_rows → 截断并回写 ``mark_truncated``；
  * 空值域 → ``SQL_SCOPE_EMPTY``，拒绝执行；
  * 写语句 → 护栏拒绝，**绝不触达真实连接**；
  * 真实只读池：写操作被数据库层拒（``READ ONLY`` 会话）。

★ 测试自备一张探针表（fixture 建/删），**不依赖任何种子数据行数**——否则「截断」用例
会随库里累计行数漂移（B2/B3 反复跑留下的 36 行曾掩盖了这个脆弱点）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from dba.modules.chatbi import build_chatbi
from dba.modules.chatbi.guard import CompiledScope
from dba.storage.mysql.engine import ReadOnlyPool, build_engine
from dba.storage.mysql.repo import SqlAuditRepo
from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.usefixtures("mysql_dsn")

#: 探针表（fixture 内建/删；偶数行 = east，奇数行 = west，共 10 行）
_TABLE = "_b4_chatbi_probe"
_SCOPE_COLUMN = "region"
_SCOPE_VALUE = "east"
_TOTAL_ROWS = 10
_SCOPE_ROWS = 5  # east 行数


class _ScopeRepo:
    """行级规则仓储替身（本文件直接把 scope 传给 executor，故这里恒返回空）。"""

    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:  # noqa: ARG002
        return []

    async def rule_version(self, role_ids: list[int]) -> str | None:  # noqa: ARG002
        return None


class _MappingRepo:
    async def all_mappings(self) -> list[dict[str, Any]]:
        return []


def _ctx(trace: str, **kw: object) -> RunContext:
    base: dict[str, object] = {"trace_id": trace, "span_id": "0" * 16, "module": "chatbi"}
    base.update(kw)
    return RunContext(**base)  # type: ignore[arg-type]


@pytest.fixture
async def chatbi_env(mysql_dsn: str):  # type: ignore[no-untyped-def]
    engine: AsyncEngine = build_engine(mysql_dsn)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLE}")
        await conn.exec_driver_sql(
            f"CREATE TABLE {_TABLE} (id INT PRIMARY KEY, region VARCHAR(16) NOT NULL)"
        )
        values = ", ".join(
            f"({i}, '{_SCOPE_VALUE if i % 2 == 0 else 'west'}')" for i in range(1, _TOTAL_ROWS + 1)
        )
        await conn.exec_driver_sql(f"INSERT INTO {_TABLE} (id, region) VALUES {values}")

    ro_pool = ReadOnlyPool(mysql_dsn, timeout_s=5)
    repos = SqlAuditRepo(engine)
    bundle = build_chatbi(
        audit=repos,
        scope_rule_repo=_ScopeRepo(),
        field_mapping_repo=_MappingRepo(),
        readonly_pool=ro_pool,
        allowed_tables={_TABLE},
        max_rows=5000,
        timeout_s=5,
    )
    try:
        yield bundle, ro_pool, repos
    finally:
        await ro_pool.aclose()
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLE}")
        await engine.dispose()


def _scope(values: list[Any]) -> CompiledScope:
    return CompiledScope(per_table={_TABLE: (_SCOPE_COLUMN, values)})


async def test_executor_injects_scope_and_audits_real_mysql(chatbi_env) -> None:  # type: ignore[no-untyped-def]
    bundle, _ro, repos = chatbi_env
    trace = "a1" + "0" * 30
    ctx = _ctx(trace, user_id=None, scope_hash="h" * 32)

    rows, meta = await bundle.executor.execute(
        f"SELECT id, {_SCOPE_COLUMN} FROM {_TABLE}",
        ctx,
        _scope([_SCOPE_VALUE]),
        max_rows=5000,
        timeout_s=5,
    )

    assert meta["scope_injected"] is True
    assert meta["truncated"] is False
    assert len(rows) == _SCOPE_ROWS  # 只返回权限值域内的行
    assert all(r[_SCOPE_COLUMN] == _SCOPE_VALUE for r in rows)

    audited = await repos.query({"trace_id": trace, "decision": "rewrite"})
    assert audited, "执行后应落 rewrite 审计"
    latest = audited[0]
    assert int(latest["scope_injected"]) == 1
    assert latest["scope_hash"] == "h" * 32
    assert latest["rewritten_sql"] and _SCOPE_COLUMN in latest["rewritten_sql"]


async def test_executor_denies_write_and_records_deny(chatbi_env) -> None:  # type: ignore[no-untyped-def]
    bundle, _ro, repos = chatbi_env
    trace = "b2" + "0" * 30

    with pytest.raises(SqlGuardError) as ei:
        await bundle.executor.execute(
            f"DELETE FROM {_TABLE}", _ctx(trace), None, max_rows=10, timeout_s=5
        )
    assert ei.value.symbol == "SQL_NOT_READONLY"

    denied = await repos.query({"trace_id": trace, "decision": "deny"})
    assert denied and denied[0]["guard_stage"] == "readonly"


async def test_executor_truncates_and_marks_real_mysql(chatbi_env) -> None:  # type: ignore[no-untyped-def]
    bundle, _ro, repos = chatbi_env
    trace = "c3" + "0" * 30

    rows, meta = await bundle.executor.execute(
        f"SELECT id, {_SCOPE_COLUMN} FROM {_TABLE}",
        _ctx(trace, user_id=None),
        _scope([_SCOPE_VALUE]),  # 5 行 east
        max_rows=3,
        timeout_s=5,
    )
    assert len(rows) == 3
    assert meta["truncated"] is True

    audited = await repos.query({"trace_id": trace, "decision": "rewrite"})
    assert audited and "truncated:rows>3" in (audited[0]["deny_reason"] or "")


async def test_executor_empty_scope_is_rejected_real_mysql(chatbi_env) -> None:  # type: ignore[no-untyped-def]
    bundle, _ro, _repos = chatbi_env
    with pytest.raises(SqlGuardError) as ei:
        await bundle.executor.execute(
            f"SELECT id FROM {_TABLE}", _ctx("d4" + "0" * 30), _scope([]), max_rows=10, timeout_s=5
        )
    assert ei.value.symbol == "SQL_SCOPE_EMPTY"


async def test_readonly_pool_rejects_writes(chatbi_env) -> None:  # type: ignore[no-untyped-def]
    # ★ 红线 6 的数据库层兜底：只读池会话 READ ONLY，写操作被真实数据库拒绝
    _bundle, ro_pool, _repos = chatbi_env
    with pytest.raises(Exception):  # noqa: B017 - 不同驱动异常类型，统一断言「抛错」
        async with ro_pool.acquire() as conn:
            await conn.exec_driver_sql(f"UPDATE {_TABLE} SET region = region WHERE id = 1")


# ═════════════════════════════════════════════════════════════════════
# ★ QA-CRITICAL-1/-2 端到端回归：维表 JOIN 注入必须挂到**维表**（真实执行 SQL）
# ═════════════════════════════════════════════════════════════════════
_FACT = "_qa_b5_fact"
_DIM = "_qa_b5_dim"
_UNREG = "_qa_b5_unreg"

# fact 自带同名列 region（碰撞诱饵）；权限列 region 由规则语义指向维表 _qa_b5_dim
_FACT_ROWS = [(1, 1, "east"), (2, 2, "east"), (3, 1, "west")]
_DIM_ROWS = [(1, "east"), (2, "west")]
# 正确语义：按维表 region，east 用户只应看到 dim_id=1 的行 → id ∈ {1, 3}
_EAST_FACT_IDS = {1, 3}


class _DimScopeRepo:
    """权限列 region 在维表上（事实表**无该列语义**，尽管物理上恰好同名）。"""

    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:  # noqa: ARG002
        return [
            {
                "enabled": 1,
                "physical_table": _FACT,
                "scope_column": "region",
                "value_type": "STATIC",
                "value_json": '["east"]',
            }
        ]

    async def rule_version(self, role_ids: list[int]) -> str | None:  # noqa: ARG002
        return "v1"


class _DimMapRepo:
    async def all_mappings(self) -> list[dict[str, Any]]:
        return [
            {
                "physical_table": _FACT,
                "join_path": {"dim_table": _DIM, "on": f"{_FACT}.dim_id = {_DIM}.id"},
            }
        ]


@pytest.fixture
async def dim_env(mysql_dsn: str) -> AsyncIterator[Any]:
    engine: AsyncEngine = build_engine(mysql_dsn)
    async with engine.begin() as conn:
        for t in (_FACT, _DIM, _UNREG):
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await conn.exec_driver_sql(f"CREATE TABLE {_DIM} (id INT PRIMARY KEY, region VARCHAR(16))")
        await conn.exec_driver_sql(
            f"CREATE TABLE {_FACT} (id INT PRIMARY KEY, dim_id INT, region VARCHAR(16))"
        )
        await conn.exec_driver_sql(f"CREATE TABLE {_UNREG} (id INT PRIMARY KEY)")
        await conn.exec_driver_sql(
            f"INSERT INTO {_DIM} VALUES " + ", ".join(f"({i},'{r}')" for i, r in _DIM_ROWS)
        )
        await conn.exec_driver_sql(
            f"INSERT INTO {_FACT} VALUES " + ", ".join(f"({i},{d},'{r}')" for i, d, r in _FACT_ROWS)
        )
        await conn.exec_driver_sql(f"INSERT INTO {_UNREG} VALUES (1)")

    ro_pool = ReadOnlyPool(mysql_dsn, timeout_s=5)
    bundle = build_chatbi(
        audit=SqlAuditRepo(engine),
        scope_rule_repo=_DimScopeRepo(),
        field_mapping_repo=_DimMapRepo(),
        readonly_pool=ro_pool,
        allowed_tables={_FACT, _DIM},  # ★ _UNREG 故意不登记
        max_rows=5000,
        timeout_s=5,
    )
    try:
        yield bundle, ro_pool
    finally:
        await ro_pool.aclose()
        async with engine.begin() as conn:
            for t in (_FACT, _DIM, _UNREG):
                await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await engine.dispose()


async def test_dim_join_projecting_dim_col_never_leaks(dim_env) -> None:  # type: ignore[no-untyped-def]
    """★ QA-CRITICAL-1：投影维表 region 时不得把谓词挂到主表同名列（真实越权泄露）。"""
    bundle, _ro = dim_env
    scope = await bundle.scope_compiler.compile(role_ids=[1])
    sql = (
        f"SELECT f.id AS id, d.region AS region FROM {_FACT} AS f "
        f"JOIN {_DIM} AS d ON f.dim_id = d.id"
    )
    rows, meta = await bundle.executor.execute(
        sql, _ctx("f1" + "0" * 30), scope, max_rows=5000, timeout_s=5
    )
    assert meta["scope_injected"] is True
    assert {r["id"] for r in rows} == _EAST_FACT_IDS, f"越权/漏注入：{rows}"
    assert {r["region"] for r in rows} == {"east"}
    assert 2 not in {r["id"] for r in rows}  # 碰撞诱饵行（dim=west 但自身 region=east）不得泄露


async def test_dim_join_without_projection_is_not_over_rejected(dim_env) -> None:  # type: ignore[no-untyped-def]
    """★ QA-CRITICAL-2：不投影 region 时注入仍须落到维表（挂主表不存在列会误拒）。"""
    bundle, _ro = dim_env
    scope = await bundle.scope_compiler.compile(role_ids=[1])
    sql = f"SELECT f.id AS id FROM {_FACT} AS f JOIN {_DIM} AS d ON f.dim_id = d.id"
    rows, _meta = await bundle.executor.execute(
        sql, _ctx("f2" + "0" * 30), scope, max_rows=5000, timeout_s=5
    )
    assert {r["id"] for r in rows} == _EAST_FACT_IDS


async def test_unregistered_table_is_rejected_e2e(dim_env) -> None:  # type: ignore[no-untyped-def]
    """Minor 补强：未登记白名单的表必须被拒（真实链路，不只单测覆盖）。"""
    bundle, _ro = dim_env
    with pytest.raises(SqlGuardError) as ei:
        await bundle.executor.execute(
            f"SELECT id FROM {_UNREG}", _ctx("f3" + "0" * 30), None, max_rows=10, timeout_s=5
        )
    assert ei.value.symbol == "SQL_TABLE_NOT_ALLOWED"
