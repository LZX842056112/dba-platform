"""``guard`` 对抗套件：行级权限 + 只读红线的**可执行证据**（§12.5）。

★ 这一套是「行级权限没有穿透」这句话的唯一凭证，因此**离线即可跑**（秒级、CI 高频），
  不依赖真实 MySQL：
  * 通过**真实护栏链** ``SqlGuardChain.validate`` 跑判定与注入改写；
  * 覆盖「5 类 SQL 形态 × 3 类角色 × N 个边界值」矩阵（本文件生成 ≥90 条）；
  * 兜底断言（``RowScopeVerifier``）与 ``scope_hash`` 串权单独直测。

★ 诚实边界：本套件断言的是**护栏判定与改写文本**（注入到正确的表/别名、是顶层 AND
  合取项、拒绝码正确）；「改写后真实行集合 ⊆ 本角色可见集」由 ``golden`` 套件（真实 MySQL）
  与 QA 的 ``var/qa/tests`` 端到端用例覆盖。dry-run 阶段用空连接桩（EXPLAIN 恒通过），
  因为它不是本套件的被测对象。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError

from dba.modules.chatbi.guard import (
    CompiledScope,
    FieldMappingJoinResolver,
    RowScopeCompiler,
    RowScopeVerifier,
    build_guard_chain,
)
from dba.modules.chatbi.guard.dry_run import ReadOnlyConnection

from .models import CaseOutcome, GuardCase, SuiteResult

__all__ = ["build_cases", "run_guard_suite"]

FACT = "eval_fact_sales"
DIM = "eval_dim_region"
CHANNEL = "eval_dim_channel"
_TABLES = {FACT, DIM, CHANNEL}

#: 角色 → 权限值域（渲染为 ``IN (...)`` 的 SQL 片段；``empty`` 表示「有规则但值域为空」）
_ROLE_VALUES: dict[str, list[str]] = {
    "east": ["east"],
    "west": ["west"],
    "global": ["east", "west", "north"],
    "empty": [],
}


def _render(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _scope_for(role: str) -> CompiledScope | None:
    values = _ROLE_VALUES.get(role)
    if values is None:
        return None
    return CompiledScope(per_table={FACT: ("region", list(values))})


# ── 测试替身 ────────────────────────────────────────────────────────
class _NullAudit:
    async def write(self, **kwargs: Any) -> int:
        _ = kwargs
        return 1

    async def mark_truncated(self, *args: Any, **kwargs: Any) -> int:
        _ = (args, kwargs)
        return 1


class _NullScopeRepo:
    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:
        _ = role_ids
        return []

    async def rule_version(self, role_ids: list[int]) -> str | None:
        _ = role_ids
        return None


class _MappingRepo:
    """把 ``FACT`` 的权限列声明在维表 ``DIM`` 上（走 join 路径）。"""

    async def all_mappings(self) -> list[dict[str, Any]]:
        return [
            {
                "physical_table": FACT,
                "join_path": {"dim_table": DIM, "on": f"{FACT}.region_id = {DIM}.id"},
            }
        ]


class _NullResult:
    def keys(self) -> list[str]:
        return []

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        _ = n
        return []


class _NullConn:
    async def exec_driver_sql(self, sql: str) -> _NullResult:
        _ = sql
        return _NullResult()


class _NullAcquire:
    async def __aenter__(self) -> _NullConn:
        return _NullConn()

    async def __aexit__(self, *args: object) -> bool:
        return False


class _NullPool:
    """只读池桩：EXPLAIN / 取数恒成功且返回空（dry-run 非本套件被测对象）。"""

    def acquire(self) -> _NullAcquire:
        return _NullAcquire()


def _make_ctx(trace: str) -> RunContext:
    return RunContext(trace_id=trace, span_id="0" * 16, module="chatbi", user_id=1)


def _build_chain() -> Any:
    return build_guard_chain(
        audit=_NullAudit(),
        scope_rule_repo=_NullScopeRepo(),
        field_mapping_repo=_MappingRepo(),
        readonly_conn=ReadOnlyConnection(_NullPool()),
        allowed_tables=set(_TABLES),
        max_rows=1000,
        timeout_s=5,
    )


def _build_resolver() -> FieldMappingJoinResolver:
    return FieldMappingJoinResolver(_MappingRepo())


# ── 用例生成 ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Form:
    """一种 SQL 形态：``qualifiers`` 是注入谓词应使用的限定名序列。"""

    tag: str
    sql: str
    qualifiers: tuple[str, ...]


_INJECT_FORMS: tuple[_Form, ...] = (
    _Form("single", f"SELECT region, SUM(amount) AS total FROM {FACT} GROUP BY region", (FACT,)),
    _Form(
        "alias",
        f"SELECT f.region, SUM(f.amount) AS total FROM {FACT} AS f GROUP BY f.region",
        ("f",),
    ),
    _Form(
        "join_dim",
        f"SELECT {DIM}.region, SUM(f.amount) AS total FROM {FACT} AS f "
        f"JOIN {DIM} ON f.region_id = {DIM}.id GROUP BY {DIM}.region",
        (DIM,),
    ),
    _Form(
        "join_dim_alias",
        f"SELECT d.region, SUM(f.amount) AS total FROM {FACT} AS f "
        f"JOIN {DIM} AS d ON f.region_id = d.id GROUP BY d.region",
        ("d",),
    ),
    _Form(
        "join_dim_left",
        f"SELECT d.region, SUM(f.amount) AS total FROM {FACT} AS f "
        f"LEFT JOIN {DIM} AS d ON f.region_id = d.id GROUP BY d.region",
        ("d",),
    ),
    _Form(
        "cte",
        f"WITH c AS (SELECT region, amount FROM {FACT}) "
        f"SELECT region, SUM(amount) AS total FROM c GROUP BY region",
        (FACT,),
    ),
    _Form(
        "cte_two",
        f"WITH a AS (SELECT region FROM {FACT}), b AS (SELECT region FROM a) SELECT region FROM b",
        (FACT,),
    ),
    _Form(
        "union",
        f"SELECT region FROM {FACT} UNION SELECT region FROM {FACT}",
        (FACT,),
    ),
    _Form(
        "union_three",
        f"SELECT region FROM {FACT} UNION SELECT region FROM {FACT} "
        f"UNION SELECT region FROM {FACT}",
        (FACT,),
    ),
    _Form(
        "subquery",
        f"SELECT t.region FROM (SELECT region, amount FROM {FACT}) AS t",
        (FACT,),
    ),
    _Form(
        "nested_two_layers",
        f"SELECT s.region FROM (SELECT t.region FROM (SELECT region FROM {FACT}) AS t) AS s",
        (FACT,),
    ),
    _Form(
        "correlated_exists",
        f"SELECT f.id FROM {FACT} AS f WHERE EXISTS "
        f"(SELECT 1 FROM {FACT} AS g WHERE g.region = f.region)",
        ("f", "g"),
    ),
    _Form(
        "in_subquery",
        f"SELECT f.id FROM {FACT} AS f WHERE f.region IN (SELECT g.region FROM {FACT} AS g)",
        ("f", "g"),
    ),
    _Form(
        "not_exists_subquery",
        f"SELECT f.region, SUM(f.amount) AS total FROM {FACT} AS f WHERE NOT EXISTS "
        f"(SELECT 1 FROM {DIM} AS c WHERE c.id = f.id) GROUP BY f.region",
        ("f",),
    ),
    _Form(
        "exists_outer_dim",
        f"SELECT f.region, SUM(f.amount) AS total FROM {FACT} AS f WHERE EXISTS "
        f"(SELECT 1 FROM {DIM} AS c WHERE c.id = f.id) GROUP BY f.region",
        ("f",),
    ),
    _Form(
        "scalar_subquery",
        f"SELECT region FROM {FACT} WHERE amount > (SELECT MAX(amount) FROM {FACT})",
        (FACT,),
    ),
    _Form(
        "alias_subquery_layer",
        f"SELECT region FROM (SELECT f.region AS region FROM {FACT} AS f) AS s",
        ("f",),
    ),
    _Form(
        "where_filter",
        f"SELECT region, amount FROM {FACT} WHERE region <> 'north'",
        (FACT,),
    ),
    _Form(
        "where_and",
        f"SELECT region, amount FROM {FACT} WHERE region = 'north' AND amount > 100",
        (FACT,),
    ),
    _Form(
        "where_or",
        f"SELECT region, amount FROM {FACT} WHERE region = 'east' OR region = 'west'",
        (FACT,),
    ),
    _Form(
        "where_in_list",
        f"SELECT region, amount FROM {FACT} WHERE region IN ('east', 'west')",
        (FACT,),
    ),
    _Form(
        "having",
        f"SELECT region, COUNT(*) AS c FROM {FACT} GROUP BY region HAVING SUM(amount) > 100",
        (FACT,),
    ),
    _Form(
        "distinct",
        f"SELECT DISTINCT region FROM {FACT}",
        (FACT,),
    ),
    _Form(
        "order_by_limit",
        f"SELECT region, amount FROM {FACT} ORDER BY amount DESC",
        (FACT,),
    ),
    _Form(
        "two_protected_scopes",
        f"SELECT f.region, {CHANNEL}.channel FROM {FACT} AS f "
        f"JOIN {CHANNEL} ON f.channel_id = {CHANNEL}.id",
        ("f",),
    ),
)


def _inject_cases() -> list[GuardCase]:
    cases: list[GuardCase] = []
    for form in _INJECT_FORMS:
        for role in ("east", "west", "global"):
            values = _ROLE_VALUES[role]
            rendered = _render(values)
            cases.append(
                GuardCase(
                    id=f"scope.inject.{form.tag}.{role}",
                    group="scope",
                    sql=form.sql,
                    role=role,
                    expected="inject",
                    must_contain=tuple(f"{q}.region IN ({rendered})" for q in form.qualifiers),
                    notes=f"{form.tag} × {role}",
                )
            )
        # 值域为空 → 必须 SQL_SCOPE_EMPTY（v1 会放行整表）
        cases.append(
            GuardCase(
                id=f"scope.empty.{form.tag}",
                group="scope",
                sql=form.sql,
                role="empty",
                expected="SQL_SCOPE_EMPTY",
                notes=f"{form.tag} × 空值域",
            )
        )
    return cases


def _deny_cases() -> list[GuardCase]:
    return [
        GuardCase(
            id="scope.deny.dim_not_joined",
            group="scope",
            sql=f"SELECT SUM(amount) AS total FROM {FACT}",
            role="east",
            expected="SQL_SCOPE_JOIN_MISSING",
            notes="权限列不在查询中且无可用 join 路径 → 拒绝（不产出非法 SQL）",
        ),
        GuardCase(
            id="readonly.write_delete",
            group="readonly",
            sql=f"DELETE FROM {FACT}",
            role="east",
            expected="SQL_NOT_READONLY",
        ),
        GuardCase(
            id="readonly.write_update",
            group="readonly",
            sql=f"UPDATE {FACT} SET amount = 0",
            role="east",
            expected="SQL_NOT_READONLY",
        ),
        GuardCase(
            id="readonly.write_drop",
            group="readonly",
            sql=f"DROP TABLE {FACT}",
            role="east",
            expected="SQL_NOT_READONLY",
        ),
        GuardCase(
            id="readonly.multi_statement",
            group="readonly",
            sql=f"SELECT region FROM {FACT}; DROP TABLE {FACT}",
            role="east",
            expected="SQL_MULTI_STATEMENT",
        ),
        GuardCase(
            id="readonly.into_outfile",
            group="readonly",
            sql=f"SELECT region FROM {FACT} INTO OUTFILE '/tmp/leak.csv'",
            role="east",
            expected="SQL_DANGEROUS_CONSTRUCT",
        ),
        GuardCase(
            id="readonly.comment_injection",
            group="readonly",
            sql="SELECT /*!50000 region */ FROM eval_fact_sales",
            role="east",
            expected="SQL_DANGEROUS_CONSTRUCT",
        ),
        GuardCase(
            id="readonly.for_update",
            group="readonly",
            sql=f"SELECT region FROM {FACT} FOR UPDATE",
            role="east",
            expected="SQL_DANGEROUS_CONSTRUCT",
        ),
        GuardCase(
            id="readonly.sleep",
            group="readonly",
            sql=f"SELECT SLEEP(3) FROM {FACT}",
            role="east",
            expected="SQL_DANGEROUS_FUNCTION",
        ),
        GuardCase(
            id="readonly.system_schema",
            group="readonly",
            sql="SELECT table_name FROM information_schema.tables",
            role="east",
            expected="SQL_TABLE_NOT_ALLOWED",
        ),
        GuardCase(
            id="readonly.table_not_allowed",
            group="readonly",
            sql="SELECT * FROM secret_payroll",
            role="east",
            expected="SQL_TABLE_NOT_ALLOWED",
        ),
        GuardCase(
            id="readonly.function_not_allowed",
            group="readonly",
            sql=f"SELECT UUID() AS u FROM {FACT}",
            role="east",
            expected="SQL_FUNCTION_NOT_ALLOWED",
        ),
    ]


_ASSERTION_SQL = f"SELECT f.id FROM {FACT} AS f WHERE "


def _assertion_cases() -> list[GuardCase]:
    return [
        GuardCase(
            id="assert.or_short_circuit",
            group="assertion",
            sql=f"{_ASSERTION_SQL}{FACT}.region = 'east' OR 1 = 1",
            role="east",
            expected="SQL_SCOPE_NOT_INJECTED",
            notes="OR 短路：scope 谓词必须在顶层 AND 链，不能被 OR 包裹",
        ),
        GuardCase(
            id="assert.is_not_null",
            group="assertion",
            sql=f"{_ASSERTION_SQL}{FACT}.region IS NOT NULL",
            role="east",
            expected="SQL_SCOPE_NOT_INJECTED",
        ),
        GuardCase(
            id="assert.unqualified_predicate",
            group="assertion",
            sql=f"{_ASSERTION_SQL}region IN ('east')",
            role="east",
            expected="SQL_SCOPE_NOT_INJECTED",
            notes="无限定 → 无法证明作用在正确的表上",
        ),
        GuardCase(
            id="assert.dangling_table",
            group="assertion",
            sql=f"{_ASSERTION_SQL}ghost_table.region IN ('east')",
            role="east",
            expected="SQL_SCOPE_NOT_INJECTED",
            notes="悬空引用：谓词引用了不在本 SELECT 里的表",
        ),
        GuardCase(
            id="assert.value_mismatch",
            group="assertion",
            sql=f"{_ASSERTION_SQL}{FACT}.region IN ('west')",
            role="east",
            expected="SQL_SCOPE_NOT_INJECTED",
            notes="取值域不匹配（用户仅授权 east）",
        ),
        GuardCase(
            id="assert.correct_conjunct_passes",
            group="assertion",
            sql=f"{_ASSERTION_SQL}{FACT}.region IN ('east')",
            role="east",
            expected="ok",
        ),
    ]


def _cache_cases() -> list[GuardCase]:
    # ``cache`` 组在 _run_case 里单独处理：只校验 scope_hash 的隔离性。
    return [
        GuardCase(
            id="cache.east_vs_west",
            group="cache",
            sql="",
            role="east|west",
            expected="differ",
        ),
        GuardCase(
            id="cache.same_role_stable",
            group="cache",
            sql="",
            role="east|east",
            expected="same",
        ),
        GuardCase(
            id="cache.empty_differs",
            group="cache",
            sql="",
            role="east|empty",
            expected="differ",
        ),
    ]


def build_cases() -> list[GuardCase]:
    """生成全部对抗用例（≥90）。"""
    cases = _inject_cases() + _deny_cases() + _assertion_cases() + _cache_cases()
    return cases


# ── 执行 ────────────────────────────────────────────────────────────
async def _run_chain_case(case: GuardCase, chain: Any) -> CaseOutcome:
    scope = _scope_for(case.role)
    ctx = _make_ctx(f"guard{abs(hash(case.id)) % (16**8):08x}")
    try:
        result = await chain.validate(case.sql, ctx, scope)
    except SqlGuardError as exc:
        if case.expected in ("inject", "ok"):
            return CaseOutcome(case.id, False, f"期望通过，实被拒绝：{exc.symbol}")
        if exc.symbol != case.expected:
            return CaseOutcome(case.id, False, f"期望 {case.expected}，实得 {exc.symbol}")
        return CaseOutcome(case.id, True, f"deny {exc.symbol}")

    if case.expected not in ("inject", "ok"):
        return CaseOutcome(case.id, False, f"期望拒绝 {case.expected}，实得放行")

    rewritten = result.rewritten_sql or case.sql
    if case.expected == "inject" and not result.scope_injected:
        return CaseOutcome(case.id, False, "未标记 scope_injected")
    for needle in case.must_contain:
        if needle not in rewritten:
            return CaseOutcome(case.id, False, f"改写缺失：{needle}｜实得：{rewritten}")
    for forbidden in case.must_not_contain:
        if forbidden in rewritten:
            return CaseOutcome(case.id, False, f"改写出现禁止项：{forbidden}")
    return CaseOutcome(case.id, True, "injected")


async def _run_assertion_case(case: GuardCase, verifier: RowScopeVerifier) -> CaseOutcome:
    scope = _scope_for(case.role)
    ctx = _make_ctx(f"assert{abs(hash(case.id)) % (16**8):08x}")
    try:
        await verifier.check(case.sql, ctx, scope)
    except SqlGuardError as exc:
        if case.expected == "ok":
            return CaseOutcome(case.id, False, f"期望通过，实被拒绝：{exc.symbol}")
        if exc.symbol != case.expected:
            return CaseOutcome(case.id, False, f"期望 {case.expected}，实得 {exc.symbol}")
        return CaseOutcome(case.id, True, f"deny {exc.symbol}")
    if case.expected != "ok":
        return CaseOutcome(case.id, False, f"期望断言拒绝 {case.expected}，实得通过")
    return CaseOutcome(case.id, True, "verified")


def _run_cache_case(case: GuardCase) -> CaseOutcome:
    left_role, right_role = case.role.split("|", 1)
    left = _scope_for(left_role)
    right = _scope_for(right_role)
    if left is None or right is None:
        return CaseOutcome(case.id, False, f"未知角色：{case.role}")
    left_hash = RowScopeCompiler.scope_hash(left)
    right_hash = RowScopeCompiler.scope_hash(right)
    if case.expected == "differ":
        ok = left_hash != right_hash
        detail = f"{left_role}={left_hash[:8]} vs {right_role}={right_hash[:8]}"
    else:
        ok = left_hash == right_hash
        detail = f"{left_role} 稳定={left_hash[:8]}"
    return CaseOutcome(case.id, ok, detail)


async def run_guard_suite() -> SuiteResult:
    """运行 guard 对抗套件，返回汇总（含分项通过率）。"""
    chain = _build_chain()
    verifier = RowScopeVerifier(_build_resolver())
    outcomes: list[CaseOutcome] = []
    groups: dict[str, list[bool]] = {}

    started = time.perf_counter()
    for case in build_cases():
        if case.group == "cache":
            outcome = _run_cache_case(case)
        elif case.group == "assertion":
            outcome = await _run_assertion_case(case, verifier)
        else:
            outcome = await _run_chain_case(case, chain)
        outcomes.append(outcome)
        groups.setdefault(case.group, []).append(outcome.passed)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = SuiteResult(name="guard", outcomes=outcomes)
    result.metrics["pass_rate"] = result.pass_rate
    readonly = groups.get("readonly", [])
    result.metrics["readonly_pass_rate"] = sum(readonly) / len(readonly) if readonly else 1.0
    scope_bits = groups.get("scope", []) + groups.get("assertion", []) + groups.get("cache", [])
    result.metrics["scope_pass_rate"] = sum(scope_bits) / len(scope_bits) if scope_bits else 1.0
    result.metrics["elapsed_ms"] = float(elapsed_ms)
    return result
