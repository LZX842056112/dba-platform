"""★ QA 独立端到端验证（C）：行级权限 + 真实 MySQL 数据 + 维表 JOIN。

不看 SQL 文本，只看「真实执行后返回的行集合」是否只含权限内 region。
重点覆盖最易漏注入的形态：**权限列在维表**（主表 JOIN 维表）。

包含两类场景：
  * 直连场景：权限列就在主表上；
  * 维表场景：权限列在维表上，需经 join_path 注入。
"""

from __future__ import annotations

from typing import Any

import pytest
from dba.modules.chatbi import build_chatbi
from dba.storage.mysql.engine import ReadOnlyPool, build_engine
from dba.storage.mysql.repo import SqlAuditRepo
from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.usefixtures("mysql_dsn")

_FACT = "_qa_fact"  # 权限列 region 在维表 _qa_dim 上
_DIM = "_qa_dim"
_MAIN = "_qa_main"  # 权限列 region 就在主表上
_ROLE_EAST = 1
_ROLE_GLOBAL = 2

_FACT_ROWS = [(1, 1, 10), (2, 1, 20), (3, 2, 30), (4, 3, 40), (5, 2, 50)]
_DIM_ROWS = [(1, "east"), (2, "west"), (3, "north")]
_MAIN_ROWS = [(1, "east", 10), (2, "east", 20), (3, "west", 30), (4, "north", 40)]

_EAST_FACT_IDS = {1, 2}  # dim_id=1 ⇔ region='east'
_ALL_FACT_IDS = {1, 2, 3, 4, 5}
_EAST_MAIN_IDS = {1, 2}


def _rule(table: str, values: str) -> dict[str, Any]:
    return {
        "enabled": 1,
        "physical_table": table,
        "scope_column": "region",
        "value_type": "STATIC",
        "value_json": values,
    }


class _ScopeRepo:
    """按 role 返回不同规则：_qa_main 的 region 在主表；_qa_fact 的 region 在维表。"""

    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:
        for rid in role_ids:
            if rid == _ROLE_EAST:
                return [_rule(_FACT, '["east"]'), _rule(_MAIN, '["east"]')]
            if rid == _ROLE_GLOBAL:
                return [
                    _rule(_FACT, '["east", "west", "north"]'),
                    _rule(_MAIN, '["east", "west", "north"]'),
                ]
        return []

    async def rule_version(self, role_ids: list[int]) -> str | None:  # noqa: ARG002
        return "v1"


class _MappingRepo:
    async def all_mappings(self) -> list[dict[str, Any]]:
        return [
            {
                "physical_table": _FACT,
                "join_path": {"dim_table": _DIM, "on": f"{_FACT}.dim_id = {_DIM}.id"},
            }
        ]


def _ctx(trace: str) -> RunContext:
    return RunContext(trace_id=trace, span_id="0" * 16, module="chatbi", user_id=None)


@pytest.fixture
async def env(mysql_dsn: str):  # type: ignore[no-untyped-def]
    engine: AsyncEngine = build_engine(mysql_dsn)
    async with engine.begin() as conn:
        for t in (_FACT, _DIM, _MAIN):
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await conn.exec_driver_sql(f"CREATE TABLE {_DIM} (id INT PRIMARY KEY, region VARCHAR(16))")
        await conn.exec_driver_sql(
            f"CREATE TABLE {_FACT} (id INT PRIMARY KEY, dim_id INT, amount INT)"
        )
        await conn.exec_driver_sql(
            f"CREATE TABLE {_MAIN} (id INT PRIMARY KEY, region VARCHAR(16), amount INT)"
        )
        await conn.exec_driver_sql(
            f"INSERT INTO {_DIM} (id, region) VALUES "
            + ", ".join(f"({i}, '{r}')" for i, r in _DIM_ROWS)
        )
        await conn.exec_driver_sql(
            f"INSERT INTO {_FACT} (id, dim_id, amount) VALUES "
            + ", ".join(f"({i}, {d}, {a})" for i, d, a in _FACT_ROWS)
        )
        await conn.exec_driver_sql(
            f"INSERT INTO {_MAIN} (id, region, amount) VALUES "
            + ", ".join(f"({i}, '{r}', {a})" for i, r, a in _MAIN_ROWS)
        )

    ro_pool = ReadOnlyPool(mysql_dsn, timeout_s=5)
    audit = SqlAuditRepo(engine)
    bundle = build_chatbi(
        audit=audit,
        scope_rule_repo=_ScopeRepo(),
        field_mapping_repo=_MappingRepo(),
        readonly_pool=ro_pool,
        allowed_tables={_FACT, _DIM, _MAIN},
        max_rows=5000,
        timeout_s=5,
    )
    try:
        yield bundle, ro_pool, audit, engine
    finally:
        await ro_pool.aclose()
        async with engine.begin() as conn:
            for t in (_FACT, _DIM, _MAIN):
                await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await engine.dispose()


# ── 直连场景：权限列在主表 ──────────────────────────────────────────────
async def test_main_table_direct_scope_only_returns_own_rows(env) -> None:  # type: ignore[no-untyped-def]
    bundle, _ro, _audit, _engine = env
    scope = await bundle.scope_compiler.compile(role_ids=[_ROLE_EAST])
    assert scope is not None
    rows, meta = await bundle.executor.execute(
        f"SELECT id, region FROM {_MAIN}", _ctx("a1" + "0" * 30), scope, max_rows=5000, timeout_s=5
    )
    assert meta["scope_injected"] is True
    assert {r["id"] for r in rows} == _EAST_MAIN_IDS
    assert {r["region"] for r in rows} == {"east"}


# ── 维表场景（权限列不在主表，需 join_path 注入）────────────────────────
_DIM_JOIN_NO_PROJ = (
    f"SELECT {_FACT}.id AS id FROM {_FACT} JOIN {_DIM} ON {_FACT}.dim_id = {_DIM}.id"
)
_DIM_JOIN_WITH_PROJ = (
    f"SELECT {_FACT}.id AS id, {_DIM}.region AS region "
    f"FROM {_FACT} JOIN {_DIM} ON {_FACT}.dim_id = {_DIM}.id"
)


async def test_dim_join_not_projecting_scope_col_returns_own_rows(env) -> None:  # type: ignore[no-untyped-def]
    """查询未投影 region 列时，join_path 注入生效 → 只返回权限内行。"""
    bundle, _ro, _audit, _engine = env
    scope = await bundle.scope_compiler.compile(role_ids=[_ROLE_EAST])
    assert scope is not None
    rows, meta = await bundle.executor.execute(
        _DIM_JOIN_NO_PROJ, _ctx("b2" + "0" * 30), scope, max_rows=5000, timeout_s=5
    )
    assert meta["scope_injected"] is True
    assert {r["id"] for r in rows} == _EAST_FACT_IDS, f"实得 {rows}"


async def test_dim_join_global_role_returns_all(env) -> None:  # type: ignore[no-untyped-def]
    bundle, _ro, _audit, _engine = env
    scope = await bundle.scope_compiler.compile(role_ids=[_ROLE_GLOBAL])
    assert scope is not None
    rows, meta = await bundle.executor.execute(
        _DIM_JOIN_NO_PROJ, _ctx("c3" + "0" * 30), scope, max_rows=5000, timeout_s=5
    )
    assert meta["scope_injected"] is True
    assert {r["id"] for r in rows} == _ALL_FACT_IDS


async def test_dim_join_projecting_scope_col_is_correct(env) -> None:  # type: ignore[no-untyped-def]
    """★ 最易漏注入形态：查询 SELECT 维表 region + 聚合主表指标（真实 ChatBI 常见 SQL）。

    回归锚点（原 QA-CRITICAL-1）：修复前注入器误把权限列挂到主表 `_qa_fact.region`
    （该列不存在）→ dry-run 拒绝；修复后应注入 `_qa_dim.region` 并只返回权限内行。
    """
    bundle, _ro, _audit, _engine = env
    scope = await bundle.scope_compiler.compile(role_ids=[_ROLE_EAST])
    assert scope is not None
    rows, meta = await bundle.executor.execute(
        _DIM_JOIN_WITH_PROJ, _ctx("d4" + "0" * 30), scope, max_rows=5000, timeout_s=5
    )
    assert meta["scope_injected"] is True
    assert {r["region"] for r in rows} == {"east"}, f"越权：{rows}"
    assert {r["id"] for r in rows} == _EAST_FACT_IDS


# ── 同名列碰撞 → 真实越权（数据泄露）──────────────────────────────────
_F2, _D2 = "_qa_f2", "_qa_d2"


class _F2ScopeRepo:
    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:
        return [_rule(_F2, '["east"]')]

    async def rule_version(self, role_ids: list[int]) -> str | None:  # noqa: ARG002
        return "v1"


class _F2MapRepo:
    async def all_mappings(self) -> list[dict[str, Any]]:
        return [
            {
                "physical_table": _F2,
                "join_path": {"dim_table": _D2, "on": f"{_F2}.dim_id = {_D2}.id"},
            }
        ]


async def test_collision_over_exposure_leak(mysql_dsn: str) -> None:  # type: ignore[no-untyped-def]
    """回归锚点（原 QA-CRITICAL-2）：主表与维表存在同名列 region 时，权限谓词必须仍挂在
    维表权限列上 → 不得泄露 `dim.region='west'` 的行。"""
    engine: AsyncEngine = build_engine(mysql_dsn)
    async with engine.begin() as conn:
        for t in (_F2, _D2):
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await conn.exec_driver_sql(f"CREATE TABLE {_D2} (id INT PRIMARY KEY, region VARCHAR(16))")
        await conn.exec_driver_sql(
            f"CREATE TABLE {_F2} (id INT PRIMARY KEY, dim_id INT, region VARCHAR(16))"
        )
        # dim: 1=east 2=west；fact 仅一行 dim_id=2（按维表应属 west，用户无权）
        await conn.exec_driver_sql(f"INSERT INTO {_D2} VALUES (1,'east'),(2,'west')")
        await conn.exec_driver_sql(f"INSERT INTO {_F2} VALUES (1,2,'east')")
    ro = ReadOnlyPool(mysql_dsn, timeout_s=5)
    audit = SqlAuditRepo(engine)
    bundle = build_chatbi(
        audit=audit,
        scope_rule_repo=_F2ScopeRepo(),
        field_mapping_repo=_F2MapRepo(),
        readonly_pool=ro,
        allowed_tables={_F2, _D2},
        max_rows=100,
        timeout_s=5,
    )
    try:
        scope = await bundle.scope_compiler.compile(role_ids=[1])
        sql = (
            f"SELECT {_F2}.id AS id, {_D2}.region AS region "
            f"FROM {_F2} JOIN {_D2} ON {_F2}.dim_id = {_D2}.id"
        )
        rows, _meta = await bundle.executor.execute(sql, _ctx("ee" + "0" * 30), scope, timeout_s=5)
        # 正确语义：唯一一行属 dim.region='west'，用户仅授权 east → 应返回 0 行
        assert rows == [], f"越权泄露：{rows}"
    finally:
        await ro.aclose()
        async with engine.begin() as conn:
            for t in (_F2, _D2):
                await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await engine.dispose()


# ── 追加对抗用例（Round 2）：进一步压测修复 ──────────────────────────────
_DIM_JOIN_WITH_ALIAS = (
    f"SELECT f.id AS id, d.region AS region FROM {_FACT} AS f JOIN {_DIM} AS d ON f.dim_id = d.id"
)


async def test_dim_join_with_alias_projecting_dim_col_is_correct(env) -> None:  # type: ignore[no-untyped-def]
    """维表起了别名时，谓词必须用别名限定（`d.region`），否则 MySQL 报未知列。"""
    bundle, _ro, audit, _engine = env
    scope = await bundle.scope_compiler.compile(role_ids=[_ROLE_EAST])
    assert scope is not None
    trace = "fa" + "0" * 30
    ctx = RunContext(trace_id=trace, span_id="0" * 16, module="chatbi")
    rows, meta = await bundle.executor.execute(
        _DIM_JOIN_WITH_ALIAS, ctx, scope, max_rows=5000, timeout_s=5
    )
    assert meta["scope_injected"] is True
    assert {r["region"] for r in rows} == {"east"}, f"越权：{rows}"
    assert {r["id"] for r in rows} == _EAST_FACT_IDS

    audited = await audit.query({"trace_id": trace, "decision": "rewrite"})
    rewritten = audited[0]["rewritten_sql"] or ""
    assert "`d`.`region`" in rewritten or "d.region" in rewritten, f"应用别名限定：{rewritten}"


async def test_dim_table_not_joined_is_rejected(env) -> None:  # type: ignore[no-untyped-def]
    """权限列在维表、但查询未 join 维表 → 无法安全注入 → 必须拒绝（不得产出非法 SQL）。"""
    bundle, _ro, _audit, _engine = env
    scope = await bundle.scope_compiler.compile(role_ids=[_ROLE_EAST])
    assert scope is not None
    with pytest.raises(SqlGuardError) as ei:
        await bundle.executor.execute(
            f"SELECT id FROM {_FACT}", _ctx("fb" + "0" * 30), scope, timeout_s=5
        )
    assert ei.value.symbol in ("SQL_SCOPE_JOIN_MISSING", "SQL_DRY_RUN_FAILED")
