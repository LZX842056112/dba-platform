"""★ B4 五道 SQL 护栏单测（《设计方案 v2》§6.3.2 / §6.3.3）。

覆盖点（每条都对应 v2 相对 v1 的一处「照抄就错」）：
  * ① 只读护栏：``sqlglot.parse`` 恰好一条 + 只读类型 + 危险构造/函数黑名单 + 表/函数白名单；
  * ② 方言护栏：目标方言往返；
  * ③ 行级注入：未配置 vs 空值域、按 SELECT 自身作用域注入、CTE/UNION/子查询、维表 join 缺失；
  * ③' 覆盖性断言：必须命中「取值等于值域」的**顶层 AND 合取项**（OR 短路、IS NOT NULL 均被拒）；
  * ④ LIMIT 归一 + ``MAX_EXECUTION_TIME`` 提示（超时双保险的 SQL 侧一环）。
"""

from __future__ import annotations

import pytest
import sqlglot
from dba.modules.chatbi.guard import (
    CompiledScope,
    DialectGuard,
    JoinPath,
    LimitGuard,
    ReadonlyGuard,
    RowScopeInjector,
    RowScopeVerifier,
    SqlGuardChain,
)
from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError
from sqlglot import exp

# ── 测试替身 ────────────────────────────────────────────────────────────
_ALLOWED = {"t_order", "t_fact", "t_dim"}


def make_ctx(**kw: object) -> RunContext:
    base: dict[str, object] = {"trace_id": "a" * 32, "span_id": "b" * 16, "module": "chatbi"}
    base.update(kw)
    return RunContext(**base)  # type: ignore[arg-type]


class _Resolver:
    """固定返回 join 路径的 ``JoinPathResolver`` 替身。"""

    def __init__(self, path: JoinPath | None) -> None:
        self._path = path

    async def join_path_for(self, table: str) -> JoinPath | None:  # noqa: ARG002
        return self._path


class _Compiler:
    """注入器不消费 compiler，占位即可。"""


class _Audit:
    """审计桩（幂等测试用）。"""

    def __init__(self) -> None:
        self.writes: list[object] = []

    async def write(self, **kw: object) -> int:
        self.writes.append(kw)
        return len(self.writes)


async def _expect_symbol(coro, symbol: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SqlGuardError) as ei:
        await coro
    assert ei.value.symbol == symbol, f"期望 {symbol}，实得 {ei.value.symbol}"


# ═════════════════════════════════════════════════════════════════════════
# ① 只读护栏
# ═════════════════════════════════════════════════════════════════════════
async def test_readonly_accepts_single_readonly_select() -> None:
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    res = await guard.check("SELECT id, COUNT(*) FROM t_order GROUP BY id", make_ctx(), None)
    assert res.ok is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM t_order WHERE id = 1 AND v = 2",
        "SELECT id FROM t_order WHERE id = 1 OR v = 2",
        "SELECT id FROM t_order WHERE NOT (id = 1)",
        "SELECT id FROM t_order WHERE dt BETWEEN '2026-01-01' AND '2026-01-31'",
        "SELECT id FROM t_order WHERE id = 1 AND dt BETWEEN '2026-01-01' AND '2026-01-31'",
    ],
)
async def test_readonly_allows_boolean_operators(sql: str) -> None:
    # ★ 回归（B6 评测发现）：sqlglot 把 ``exp.And`` / ``exp.Or`` / ``exp.Not`` 也归入
    #   ``exp.Func``，其 ``sql_name()`` 为 ``'AND'`` / ``'OR'`` / ``'NOT'``。若不排除，
    #   **任何带多条件 WHERE 的 SQL**（含行级注入后的第二次校验）都会被误判为
    #   「使用了白名单外的函数：['AND']」而拒绝。布尔连接词是**运算符**，必须放行。
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    res = await guard.check(sql, make_ctx(), None)
    assert res.ok is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM t_order WHERE EXISTS (SELECT 1 FROM t_order AS g WHERE g.id = t_order.id)",
        "SELECT id FROM t_order WHERE NOT EXISTS "
        "(SELECT 1 FROM t_order AS g WHERE g.id = t_order.id)",
        "SELECT id FROM t_order WHERE id IN (SELECT id FROM t_order)",
    ],
)
async def test_readonly_allows_subquery_predicates(sql: str) -> None:
    # ★ 回归（B6 复核）：``EXISTS`` 与 ``AND`` 是**同一个坑**——sqlglot 把谓词构造都归入
    #   ``exp.Func``（``exp.Exists`` 的 MRO 为 ``Exists → Func → SubqueryPredicate → Predicate``），
    #   其 ``sql_name()='EXISTS'``。若只排布尔连接词而漏 ``Exists``，则**任何子查询存在谓词**
    #   都被拒；而 ``IN (子查询)``（``exp.In`` 不是 ``Func``）却已放行——同一安全剖面一放一拒，
    #   本身就是不一致。函数白名单的职责是拦「危险构造」，不是拦合法读语法。
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    res = await guard.check(sql, make_ctx(), None)
    assert res.ok is True


async def test_readonly_still_rejects_unknown_function_inside_subquery() -> None:
    # ★ 反面对照：放行 ``EXISTS`` **不等于**放宽函数白名单——子查询里的白名单外函数仍必须被拒
    #   （证明排除的是「谓词构造」，而不是把子查询整体开了洞）。
    guard = ReadonlyGuard(allowed_tables={"t_order"})
    await _expect_symbol(
        guard.check(
            "SELECT id FROM t_order WHERE EXISTS (SELECT evil_func(id) FROM t_order)",
            make_ctx(),
            None,
        ),
        "SQL_FUNCTION_NOT_ALLOWED",
    )


async def test_readonly_rejects_multi_statement() -> None:
    # ★ v1 用 parse_one 只看第一条；v2 要求「恰好一条」，第二条 DROP 必须被拦
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    await _expect_symbol(
        guard.check("SELECT 1; DROP TABLE t_order", make_ctx(), None), "SQL_MULTI_STATEMENT"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t_order",
        "UPDATE t_order SET v = 1",
        "INSERT INTO t_order (id) VALUES (1)",
        "DROP TABLE t_order",
    ],
)
async def test_readonly_rejects_write_statements(sql: str) -> None:
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    await _expect_symbol(guard.check(sql, make_ctx(), None), "SQL_NOT_READONLY")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t_order INTO OUTFILE '/tmp/x'",
        "SELECT * FROM t_order INTO DUMPFILE '/tmp/x'",
        "SELECT * FROM t_order FOR UPDATE",
        "SELECT * FROM t_order LOCK IN SHARE MODE",
        "SELECT @a := 1",
        "SELECT /*! 40001 SQL_NO_CACHE */ * FROM t_order",
    ],
)
async def test_readonly_rejects_dangerous_constructs(sql: str) -> None:
    # ★ 这些构造 sqlglot 会直接抛 ParseError/TokenError；若只靠解析会退化成可回退的
    #   SQL_PARSE_ERROR，故 v2 在文本层**先于解析**硬拒（SQL_DANGEROUS_CONSTRUCT）。
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    await _expect_symbol(guard.check(sql, make_ctx(), None), "SQL_DANGEROUS_CONSTRUCT")


@pytest.mark.parametrize(
    "sql",
    ["SELECT sleep(1)", "SELECT BENCHMARK(1000000, MD5('x'))", "SELECT load_file('/etc/passwd')"],
)
async def test_readonly_rejects_dangerous_functions(sql: str) -> None:
    guard = ReadonlyGuard(allowed_tables=set(_ALLOWED))
    await _expect_symbol(guard.check(sql, make_ctx(), None), "SQL_DANGEROUS_FUNCTION")


async def test_readonly_rejects_system_schema_even_when_unrestricted() -> None:
    # ★ 白名单思路：即便没配表白名单，系统库读取同样越权，必须拒
    guard = ReadonlyGuard(allowed_tables=None)
    await _expect_symbol(
        guard.check("SELECT * FROM information_schema.tables", make_ctx(), None),
        "SQL_TABLE_NOT_ALLOWED",
    )


async def test_readonly_rejects_table_outside_whitelist() -> None:
    guard = ReadonlyGuard(allowed_tables={"t_order"})
    await _expect_symbol(
        guard.check("SELECT * FROM t_secret", make_ctx(), None), "SQL_TABLE_NOT_ALLOWED"
    )


async def test_readonly_rejects_function_outside_whitelist() -> None:
    guard = ReadonlyGuard(allowed_tables={"t_order"})
    await _expect_symbol(
        guard.check("SELECT evil_func(id) FROM t_order", make_ctx(), None),
        "SQL_FUNCTION_NOT_ALLOWED",
    )


async def test_readonly_allows_extra_function_when_configured() -> None:
    guard = ReadonlyGuard(allowed_tables={"t_order"}, extra_functions={"GREATEST"})
    res = await guard.check("SELECT GREATEST(id, 1) FROM t_order", make_ctx(), None)
    assert res.ok is True


# ═════════════════════════════════════════════════════════════════════════
# ② 方言护栏
# ═════════════════════════════════════════════════════════════════════════
async def test_dialect_accepts_expressible_sql() -> None:
    res = await DialectGuard().check("SELECT id FROM t_order LIMIT 10", make_ctx(), None)
    assert res.ok is True


async def test_dialect_rejects_unparseable_sql() -> None:
    await _expect_symbol(DialectGuard().check("SELECT (((", make_ctx(), None), "SQL_PARSE_ERROR")


# ═════════════════════════════════════════════════════════════════════════
# ③ 行级权限注入（权威防线）
# ═════════════════════════════════════════════════════════════════════════
def _scope_east() -> CompiledScope:
    return CompiledScope(per_table={"t_order": ("region", ["east"])})


async def test_inject_no_rules_passes_without_rewrite() -> None:
    # scope=None ⇔ 该用户「未配置行级规则」→ 放行（与「值域为空」严格区分）
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    res = await inj.check("SELECT id FROM t_order", make_ctx(), None)
    assert res.ok is True
    assert res.scope_injected is False


async def test_inject_empty_value_set_is_rejected() -> None:
    # ★ v1 把「未配置」与「值域为空」同等放行 → 该用户一行都不该看却拿到全表；
    #   v2 值域为空 → SQL_SCOPE_EMPTY 拒绝
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    empty = CompiledScope(per_table={"t_order": ("region", [])})
    await _expect_symbol(inj.check("SELECT id FROM t_order", make_ctx(), empty), "SQL_SCOPE_EMPTY")


async def test_inject_actually_lands_in_sql() -> None:
    # ★ 回归：sqlglot 的 ``Select.where()`` 默认 ``copy=True`` 会静默丢弃注入；
    #   本用例确保谓词真的落到 SQL 上（否则行级权限形同虚设且无任何报错）。
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    res = await inj.check("SELECT region, amount FROM t_order", make_ctx(), _scope_east())
    assert res.scope_injected is True
    assert res.rewritten_sql is not None
    assert "t_order.region IN ('east')" in res.rewritten_sql


async def test_inject_into_cte_own_scope() -> None:
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    sql = "WITH c AS (SELECT region FROM t_order) SELECT * FROM c"
    res = await inj.check(sql, make_ctx(), _scope_east())
    assert res.rewritten_sql is not None
    # 谓词注入到 CTE 内层 SELECT（自身作用域），而非外层（外层根本没有 t_order）
    assert "region IN ('east')" in res.rewritten_sql


async def test_inject_into_every_union_branch() -> None:
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    sql = "SELECT region FROM t_order UNION SELECT region FROM t_order"
    res = await inj.check(sql, make_ctx(), _scope_east())
    assert res.rewritten_sql is not None
    assert res.rewritten_sql.count("region IN ('east')") == 2


async def test_inject_dim_table_via_join_path() -> None:
    # ★ QA-MAJOR-1 修复：维表**必须真的出现在查询里**，才允许把谓词挂到维表；
    #   断言改写文本的同时，SQL 形态本身是可执行的（维表已在 FROM/JOIN 中）。
    inj = RowScopeInjector(
        _Compiler(),
        _Resolver(JoinPath(dim_table="t_dim", on="t_fact.dim_id = t_dim.id")),  # type: ignore[arg-type]
    )
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = "SELECT f.id FROM t_fact AS f JOIN t_dim AS d ON f.dim_id = d.id"
    res = await inj.check(sql, make_ctx(), scope)
    assert res.scope_injected is True
    assert res.rewritten_sql is not None
    # 维表被起了别名 d → 限定名必须用别名，否则 `t_dim.region` 在 MySQL 里是「未知列」
    assert "d.region IN ('east')" in res.rewritten_sql


async def test_inject_dim_table_not_joined_is_rejected() -> None:
    # 配了 join_path 但查询根本没 join 维表 → 注入维表会产出引用不存在表的 SQL
    # （QA-MAJOR-1 的假绿根因）→ 必须**拒绝**，而不是「改写成功却不可执行」。
    inj = RowScopeInjector(
        _Compiler(),
        _Resolver(JoinPath(dim_table="t_dim", on="t_fact.dim_id = t_dim.id")),  # type: ignore[arg-type]
    )
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    await _expect_symbol(
        inj.check("SELECT id FROM t_fact", make_ctx(), scope), "SQL_SCOPE_JOIN_MISSING"
    )


async def test_inject_projection_of_dim_col_uses_dim_path_not_fact() -> None:
    # ★ QA-CRITICAL-1/-2 回归（AST 层）：投影的是 `d.region` 时，谓词必须挂到**维表**，
    #   绝不能挂到主表同名列（不存在 → 误拒；恰好同名 → 真实越权泄露）。
    inj = RowScopeInjector(
        _Compiler(),
        _Resolver(JoinPath(dim_table="t_dim", on="t_fact.dim_id = t_dim.id")),  # type: ignore[arg-type]
    )
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = (
        "SELECT f.id AS id, t_dim.region AS region "
        "FROM t_fact AS f JOIN t_dim ON f.dim_id = t_dim.id"
    )
    res = await inj.check(sql, make_ctx(), scope)
    assert res.rewritten_sql is not None
    assert "t_dim.region IN ('east')" in res.rewritten_sql
    assert "t_fact.region IN ('east')" not in res.rewritten_sql


async def test_verify_requires_table_qualified_predicate() -> None:
    # ★ 兜底断言表感知（修复前只比列名）：
    #   * 无限定 → 拒（无法证明作用在正确的表上）；
    #   * 引用了不在本 SELECT 里的表（悬空，如 QA-MAJOR-1）→ 拒；
    #   * 正确限定到受保护表 → 通过。
    verifier = RowScopeVerifier(_Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    base = "SELECT f.id FROM t_fact AS f JOIN t_dim AS d ON f.dim_id = d.id "
    await _expect_symbol(
        verifier.check(f"{base}WHERE region IN ('east')", make_ctx(), scope),
        "SQL_SCOPE_NOT_INJECTED",
    )
    await _expect_symbol(
        verifier.check(f"{base}WHERE t_ghost.region IN ('east')", make_ctx(), scope),
        "SQL_SCOPE_NOT_INJECTED",
    )
    assert (await verifier.check(f"{base}WHERE t_fact.region IN ('east')", make_ctx(), scope)).ok


async def test_inject_dim_table_missing_join_path_is_rejected() -> None:
    # 权限列不在事实表上、又缺 join 路径 → 无法安全注入 → 拒绝（绝不「无校验放行」）
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    await _expect_symbol(
        inj.check("SELECT id FROM t_fact", make_ctx(), scope), "SQL_SCOPE_JOIN_MISSING"
    )


async def test_inject_zero_predicates_is_rejected() -> None:
    # ★ 缺陷 3「静默空注入」fail-closed 单测：受保护表已在查询中、target 也已解析出来，
    #   但**没有任何可注入的 SELECT 作用域**（`_inject` 返回 0，例如非 SELECT 语句没有
    #   `exp.Select` 祖先）。此时**绝不能**返回 ok=True/scope_injected=True 放行——
    #   那等于「宣称已注入却一个谓词都没加」。注入器自身必须先拒绝（不依赖 Verifier 兜底）。
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    # UPDATE 里的 region 是无限定列 → target 解析为 t_fact；但 UPDATE 无 Select 祖先 → 注入 0
    await _expect_symbol(
        inj.check("UPDATE t_fact SET region = 'east'", make_ctx(), scope),
        "SQL_SCOPE_NOT_INJECTED",
    )


# ═════════════════════════════════════════════════════════════════════════
# ③'' EXISTS 子查询覆盖（放行 EXISTS 的**硬前提**回归，B6 复核）
#
# 背景：放行 ``EXISTS`` 后，若其**内层 SELECT 逃过注入**，就等于开了一条越权通道。
# 故下面 4 条必须全绿；其中 (c) 是判据——若覆盖断言不下探子查询，(c) 会失败，
# 此时**不得放行 EXISTS**（fail-closed）。
# ═════════════════════════════════════════════════════════════════════════
async def test_exists_inject_outer_protected_table() -> None:
    """(a) 外层受保护表 + ``WHERE EXISTS (SELECT ... FROM 未受保护表 ...)`` → 外层被注入。"""
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = (
        "SELECT f.region, SUM(f.amount) AS total FROM t_fact AS f "
        "WHERE EXISTS (SELECT 1 FROM t_dim AS d WHERE d.id = f.dim_id) "
        "GROUP BY f.region"
    )
    res = await inj.check(sql, make_ctx(), scope)
    assert res.scope_injected is True
    assert res.rewritten_sql is not None
    rewritten = res.rewritten_sql
    assert "f.region IN ('east')" in rewritten
    # 注入被 AND 到**外层** WHERE（EXISTS 子查询之外）：
    # 子查询内不含权限谓词，谓词只在第一个 ')' （EXISTS 收尾）之后出现。
    inside_exists, _, outside_exists = rewritten.partition(")")
    assert "region IN ('east')" not in inside_exists
    assert "region IN ('east')" in outside_exists
    await RowScopeVerifier().check(rewritten, make_ctx(), scope)


async def test_exists_inject_inner_protected_table() -> None:
    """(b) ``WHERE EXISTS (SELECT ... FROM 受保护表 ...)`` → **内层 SELECT 也被注入**。"""
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = (
        "SELECT d.id FROM t_dim AS d WHERE EXISTS (SELECT 1 FROM t_fact AS f WHERE f.region = d.id)"
    )
    res = await inj.check(sql, make_ctx(), scope)
    assert res.scope_injected is True
    assert res.rewritten_sql is not None
    rewritten = res.rewritten_sql
    # 谓词被 AND 到**子查询内层** SELECT 的 WHERE 上（内层作用域）——证明注入下探到子查询
    assert "WHERE f.region = d.id AND f.region IN ('east')" in rewritten
    assert rewritten.count("region IN ('east')") == 1
    await RowScopeVerifier().check(rewritten, make_ctx(), scope)


async def test_verify_descends_into_exists_subquery() -> None:
    """(c) 反向验证：人为去掉**内层**注入 → ``RowScopeVerifier`` 必须拦住。

    这是「放行 EXISTS」的判据：只有覆盖断言**逐 SELECT**（含子查询）校验，(c) 才会拒。
    若断言只看顶层 SELECT，本用例会**静默放行**（顶层 WHERE 里根本没有权限谓词）。
    """
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = (
        "SELECT d.id FROM t_dim AS d WHERE EXISTS (SELECT 1 FROM t_fact AS f WHERE f.region = d.id)"
    )
    injected = (await inj.check(sql, make_ctx(), scope)).rewritten_sql
    assert injected is not None
    assert "f.region IN ('east')" in injected

    # 人为剥离内层注入的权限合取项（保留其余条件）
    tree = sqlglot.parse_one(injected, dialect="mysql")

    def _flatten(node: exp.Expression) -> list[exp.Expression]:
        if isinstance(node, exp.And):
            return _flatten(node.this) + _flatten(node.expression)
        return [node]

    removed = 0
    for sel in tree.find_all(exp.Select):
        where = sel.args.get("where")
        if not isinstance(where, exp.Where):
            continue
        kept = [
            c
            for c in _flatten(where.this)
            if not (
                isinstance(c, exp.In) and isinstance(c.this, exp.Column) and c.this.name == "region"
            )
        ]
        if len(kept) == len(_flatten(where.this)):
            continue
        removed += 1
        merged: exp.Expression | None = None
        for conjunct in kept:
            merged = conjunct if merged is None else exp.And(this=merged, expression=conjunct)
        sel.set("where", exp.Where(this=merged) if merged is not None else None)
    assert removed == 1, "应恰好剥离 1 处（内层）注入"

    stripped = tree.sql(dialect="mysql")
    assert "region IN ('east')" not in stripped, "权限谓词应已被剥离干净"

    # ★ 若覆盖断言只覆盖顶层 SELECT，本 SQL 会被放行（顶层无权限谓词）；
    #   必须拒绝——证明断言真的下探到了子查询。
    await _expect_symbol(
        RowScopeVerifier().check(stripped, make_ctx(), scope), "SQL_SCOPE_NOT_INJECTED"
    )


async def test_not_exists_inject_outer_protected_table() -> None:
    """(d) ``NOT EXISTS`` 同样形态：谓词构造排除需一并覆盖，且外层注入照常生效。"""
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = CompiledScope(per_table={"t_fact": ("region", ["east"])})
    sql = (
        "SELECT f.region, SUM(f.amount) AS total FROM t_fact AS f "
        "WHERE NOT EXISTS (SELECT 1 FROM t_dim AS d WHERE d.id = f.dim_id) "
        "GROUP BY f.region"
    )
    res = await inj.check(sql, make_ctx(), scope)
    assert res.scope_injected is True
    assert res.rewritten_sql is not None
    rewritten = res.rewritten_sql
    assert "f.region IN ('east')" in rewritten
    inside_exists, _, outside_exists = rewritten.partition(")")
    assert "region IN ('east')" not in inside_exists
    assert "region IN ('east')" in outside_exists
    await RowScopeVerifier().check(rewritten, make_ctx(), scope)


async def test_inject_is_idempotent_on_repeated_calls() -> None:
    # ★ 注入器幂等回归：护栏链「跑两遍」是**常态**——``sql_guard`` 步骤改写一次，
    #   ``sql_exec`` 经 ``QueryExecutor`` 再校验**同一条已改写 SQL** 时又走一次注入。
    #   注入器若没有「顶层 AND 链上已存在等价 scope 合取项则跳过」的判断，谓词会被
    #   重复追加（``X AND X`` 语义虽不变，但文本不再幂等、白白膨胀审计/回放内容）。
    #   判据必须复用 Verifier 的 ``_conjunct_matches``（同源），不另写一套等价判据。
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = _scope_east()
    first = await inj.check("SELECT region, amount FROM t_order", make_ctx(), scope)
    assert first.rewritten_sql is not None
    assert first.rewritten_sql.count("t_order.region IN ('east')") == 1
    assert first.scope_injected is True

    second = await inj.check(first.rewritten_sql, make_ctx(), scope)
    assert second.rewritten_sql is not None
    # 第二次不得再追加；且第二次仍必须报告 scope_injected=True（已满足，不是「空注入」）
    assert second.rewritten_sql.count("t_order.region IN ('east')") == 1
    assert second.rewritten_sql == first.rewritten_sql
    assert second.scope_injected is True


async def test_inject_idempotent_across_multiple_scopes() -> None:
    # 幂等需按「每个 SELECT 作用域」分别成立（UNION 两个分支各一个作用域）
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    scope = _scope_east()
    first = await inj.check(
        "SELECT region FROM t_order UNION SELECT region FROM t_order", make_ctx(), scope
    )
    assert first.rewritten_sql is not None
    assert first.rewritten_sql.count("region IN ('east')") == 2
    second = await inj.check(first.rewritten_sql, make_ctx(), scope)
    assert second.rewritten_sql is not None
    assert second.rewritten_sql.count("region IN ('east')") == 2
    assert second.rewritten_sql == first.rewritten_sql


# ═════════════════════════════════════════════════════════════════════════
# ③' 覆盖性断言（兜底防线）
# ═════════════════════════════════════════════════════════════════════════
async def test_verify_passes_after_injection() -> None:
    inj = RowScopeInjector(_Compiler(), _Resolver(None))  # type: ignore[arg-type]
    res = await inj.check("SELECT region FROM t_order", make_ctx(), _scope_east())
    assert res.rewritten_sql is not None
    ver = await RowScopeVerifier().check(res.rewritten_sql, make_ctx(), _scope_east())
    assert ver.ok is True


async def test_verify_rejects_or_short_circuit() -> None:
    # ★ v1 断言「出现过 scope 列名」是假断言；``... OR 1 = 1`` 能过 v1、必须被 v2 拒
    sql = "SELECT id FROM t_order WHERE region = 'east' OR 1 = 1"
    await _expect_symbol(
        RowScopeVerifier().check(sql, make_ctx(), _scope_east()), "SQL_SCOPE_NOT_INJECTED"
    )


async def test_verify_rejects_is_not_null() -> None:
    sql = "SELECT id FROM t_order WHERE region IS NOT NULL"
    await _expect_symbol(
        RowScopeVerifier().check(sql, make_ctx(), _scope_east()), "SQL_SCOPE_NOT_INJECTED"
    )


async def test_verify_rejects_value_mismatch() -> None:
    # 列名出现、但取值域与权限值域不一致（越权取值）→ 拒
    sql = "SELECT id FROM t_order WHERE region IN ('west')"
    await _expect_symbol(
        RowScopeVerifier().check(sql, make_ctx(), _scope_east()), "SQL_SCOPE_NOT_INJECTED"
    )


async def test_verify_accepts_matching_top_level_conjunct() -> None:
    sql = "SELECT id FROM t_order WHERE t_order.region IN ('east')"
    ver = await RowScopeVerifier().check(sql, make_ctx(), _scope_east())
    assert ver.ok is True


async def test_verify_empty_value_set_is_rejected() -> None:
    empty = CompiledScope(per_table={"t_order": ("region", [])})
    await _expect_symbol(
        RowScopeVerifier().check("SELECT id FROM t_order", make_ctx(), empty),
        "SQL_SCOPE_EMPTY",
    )


# ═════════════════════════════════════════════════════════════════════════
# ④ LIMIT + MAX_EXECUTION_TIME
# ═════════════════════════════════════════════════════════════════════════
async def test_limit_added_when_absent() -> None:
    res = await LimitGuard(max_rows=100, timeout_s=5).check(
        "SELECT id FROM t_order", make_ctx(), None
    )
    assert res.rewritten_sql is not None
    assert "LIMIT 100" in res.rewritten_sql


async def test_limit_compressed_when_over_max() -> None:
    res = await LimitGuard(max_rows=100, timeout_s=5).check(
        "SELECT id FROM t_order LIMIT 100000", make_ctx(), None
    )
    assert res.rewritten_sql is not None
    assert "LIMIT 100" in res.rewritten_sql
    assert "100000" not in res.rewritten_sql


async def test_limit_kept_when_under_max() -> None:
    res = await LimitGuard(max_rows=100, timeout_s=5).check(
        "SELECT id FROM t_order LIMIT 10", make_ctx(), None
    )
    assert res.rewritten_sql is not None
    assert "LIMIT 10" in res.rewritten_sql


async def test_max_execution_time_hint_is_attached() -> None:
    res = await LimitGuard(max_rows=100, timeout_s=5).check(
        "SELECT id FROM t_order", make_ctx(), None
    )
    assert res.rewritten_sql is not None
    assert "MAX_EXECUTION_TIME(5000)" in res.rewritten_sql


# ═════════════════════════════════════════════════════════════════════════
# 幂等性回归（护栏链必须能重复校验「已被本链改写过的 SQL」）
# ═════════════════════════════════════════════════════════════════════════
async def test_limit_output_is_reaccepted_by_readonly_guard() -> None:
    # ★ 回归：④ 注入的 ``MAX_EXECUTION_TIME`` 优化器提示，在 ① 侧会被 sqlglot 解析成
    #   ``Anonymous`` 函数。若 ① 不排除 ``exp.Hint`` 内节点，护栏链第二次执行
    #   （sql_guard 通过后 sql_exec 再校验同一 SQL）会误判「白名单外函数」而失败。
    lim = LimitGuard(max_rows=100, timeout_s=5)
    rewritten = (await lim.check("SELECT region FROM t_order", make_ctx(), None)).rewritten_sql
    assert rewritten is not None
    again = await ReadonlyGuard(allowed_tables={"t_order"}).check(rewritten, make_ctx(), None)
    assert again.ok is True


async def test_guard_chain_is_idempotent_on_second_pass() -> None:
    # ★ 链级幂等（比注入器级更有价值）：完整链条连续跑两遍，第二次输出必须与第一次
    #   **字符串级完全相等**。这一条同时守住两个不变量：
    #     ① 注入器幂等（否则行级权限谓词被重复追加）；
    #     ② ``LimitGuard`` 的 ``MAX_EXECUTION_TIME`` 优化器提示幂等（否则提示重复挂）。
    chain = SqlGuardChain(
        [
            ReadonlyGuard(allowed_tables={"t_order"}),
            DialectGuard(),
            RowScopeInjector(_Compiler(), _Resolver(None)),  # type: ignore[arg-type]
            RowScopeVerifier(),
            LimitGuard(max_rows=100, timeout_s=5),
        ],
        _Audit(),  # type: ignore[arg-type]
    )
    first = await chain.validate("SELECT region FROM t_order", make_ctx(), _scope_east())
    assert first.scope_injected is True
    assert first.rewritten_sql is not None
    assert first.rewritten_sql.count("region IN ('east')") == 1
    assert first.rewritten_sql.count("MAX_EXECUTION_TIME") == 1

    # 第二次校验「已注入权限 + 已挂提示」的 SQL：结果应稳定、不抛错、且文本完全相同
    second = await chain.validate(first.rewritten_sql, make_ctx(), _scope_east())
    assert second.ok is True
    assert second.scope_injected is True
    assert second.rewritten_sql == first.rewritten_sql
    assert second.rewritten_sql is not None
    assert second.rewritten_sql.count("region IN ('east')") == 1
    assert second.rewritten_sql.count("MAX_EXECUTION_TIME") == 1
