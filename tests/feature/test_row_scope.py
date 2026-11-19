"""FR-T02：行级权限注入特征测试。"""

from __future__ import annotations

import unittest

from dba.modules.chatbi.guard import (
    CompiledScope,
    FieldMappingJoinResolver,
    RowScopeCompiler,
    RowScopeInjector,
    RowScopeVerifier,
    SqlGuardError,
)

from tests.feature.support import (
    FakeMappingRepo,
    FakeScopeRuleRepo,
    make_ctx,
)

RULE = {
    "enabled": 1,
    "physical_table": "fact_sales",
    "scope_column": "business_line",
    "value_type": "CTX_VAR",
    "ctx_var": "business_line",
}


def scope_for(values: list[object]) -> CompiledScope:
    return CompiledScope(per_table={"fact_sales": ("business_line", values)})


class RowScopeCompilerTest(unittest.IsolatedAsyncioTestCase):
    async def test_compiles_ctx_var(self) -> None:
        compiler = RowScopeCompiler(FakeScopeRuleRepo([RULE]))
        scope = await compiler.compile(role_ids=[1], user_vars={"business_line": 1})
        self.assertIsNotNone(scope)
        self.assertEqual(scope.per_table, {"fact_sales": ("business_line", [1])})
        self.assertFalse(scope.is_empty())
        self.assertEqual(scope.rule_version, "v1")

    async def test_returns_none_without_rules(self) -> None:
        compiler = RowScopeCompiler(FakeScopeRuleRepo([]))
        scope = await compiler.compile(role_ids=[1], user_vars={"business_line": 1})
        self.assertIsNone(scope)

    async def test_empty_values_are_empty_scope(self) -> None:
        compiler = RowScopeCompiler(FakeScopeRuleRepo([RULE]))
        scope = await compiler.compile(role_ids=[1], user_vars={})
        self.assertIsNotNone(scope)
        self.assertTrue(scope.is_empty())

    async def test_compiles_multiple_ctx_values(self) -> None:
        compiler = RowScopeCompiler(FakeScopeRuleRepo([RULE]))
        scope = await compiler.compile(
            role_ids=[1], user_vars={"business_line": [1, 2]}
        )
        self.assertIsNotNone(scope)
        self.assertEqual(scope.per_table, {"fact_sales": ("business_line", [1, 2])})

    async def test_first_enabled_rule_wins(self) -> None:
        rules = [
            {
                "enabled": 0,
                "physical_table": "fact_sales",
                "scope_column": "region_id",
                "value_type": "STATIC",
                "value_json": "[9]",
            },
            RULE,
            {
                "enabled": 1,
                "physical_table": "fact_sales",
                "scope_column": "region_id",
                "value_type": "STATIC",
                "value_json": "[9]",
            },
        ]
        compiler = RowScopeCompiler(FakeScopeRuleRepo(rules))
        scope = await compiler.compile(role_ids=[1], user_vars={"business_line": 2})
        self.assertIsNotNone(scope)
        self.assertEqual(scope.per_table, {"fact_sales": ("business_line", [2])})

    async def test_compiles_multiple_tables(self) -> None:
        dim_rule = {
            "enabled": 1,
            "physical_table": "dim_region",
            "scope_column": "region_id",
            "value_type": "CTX_VAR",
            "ctx_var": "region_id",
        }
        compiler = RowScopeCompiler(FakeScopeRuleRepo([RULE, dim_rule]))
        scope = await compiler.compile(
            role_ids=[1],
            user_vars={"business_line": 1, "region_id": 10},
        )
        self.assertIsNotNone(scope)
        self.assertEqual(
            scope.per_table,
            {
                "fact_sales": ("business_line", [1]),
                "dim_region": ("region_id", [10]),
            },
        )


class RowScopeInjectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.injector = RowScopeInjector(
            None,
            FieldMappingJoinResolver(FakeMappingRepo()),
        )
        self.scope = scope_for([1])

    async def test_injects_predicate(self) -> None:
        result = await self.injector.check(
            "SELECT business_line, SUM(amount) FROM fact_sales GROUP BY business_line",
            self.ctx,
            self.scope,
        )
        self.assertTrue(result.scope_injected)
        self.assertIn("fact_sales.business_line IN (1)", result.rewritten_sql)

    async def test_injects_with_alias(self) -> None:
        result = await self.injector.check(
            "SELECT f.business_line FROM fact_sales AS f",
            self.ctx,
            self.scope,
        )
        self.assertIn("f.business_line IN (1)", result.rewritten_sql)

    async def test_injects_multiple_values_with_alias(self) -> None:
        result = await self.injector.check(
            "SELECT f.business_line FROM fact_sales AS f",
            self.ctx,
            scope_for([1, 2]),
        )
        self.assertIn("f.business_line IN (1, 2)", result.rewritten_sql)

    async def test_injects_multiple_tables(self) -> None:
        scope = CompiledScope(
            per_table={
                "fact_sales": ("business_line", [1]),
                "dim_region": ("region_id", [10]),
            }
        )
        result = await self.injector.check(
            "SELECT f.business_line, d.region_id "
            "FROM fact_sales AS f "
            "JOIN dim_region AS d ON f.region_id = d.region_id",
            self.ctx,
            scope,
        )
        self.assertIn("f.business_line IN (1)", result.rewritten_sql)
        self.assertIn("d.region_id IN (10)", result.rewritten_sql)

    async def test_empty_scope_in_one_table_denies_all(self) -> None:
        scope = CompiledScope(
            per_table={
                "fact_sales": ("business_line", []),
                "dim_region": ("region_id", [10]),
            }
        )
        with self.assertRaises(SqlGuardError) as raised:
            await self.injector.check(
                "SELECT f.business_line FROM fact_sales AS f",
                self.ctx,
                scope,
            )
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_EMPTY")

    async def test_injects_into_cte(self) -> None:
        result = await self.injector.check(
            "WITH t AS (SELECT business_line, amount FROM fact_sales) "
            "SELECT business_line, SUM(amount) FROM t GROUP BY business_line",
            self.ctx,
            self.scope,
        )
        self.assertIn(
            "fact_sales WHERE fact_sales.business_line IN (1)",
            result.rewritten_sql,
        )

    async def test_injects_into_union_branches(self) -> None:
        result = await self.injector.check(
            "SELECT business_line FROM fact_sales UNION ALL "
            "SELECT business_line FROM fact_sales",
            self.ctx,
            self.scope,
        )
        self.assertEqual(result.rewritten_sql.count("business_line IN (1)"), 2)

    async def test_star_query_without_join_path_is_denied(self) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.injector.check("SELECT * FROM fact_sales", self.ctx, self.scope)
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_JOIN_MISSING")

    async def test_empty_scope_is_denied(self) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.injector.check(
                "SELECT business_line FROM fact_sales",
                self.ctx,
                scope_for([]),
            )
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_EMPTY")

    async def test_no_scope_is_allowed(self) -> None:
        result = await self.injector.check(
            "SELECT business_line FROM fact_sales",
            self.ctx,
            None,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.scope_injected)


class RowScopeVerifierTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.verifier = RowScopeVerifier()
        self.scope = scope_for([1])

    async def test_accepts_injected_predicate(self) -> None:
        result = await self.verifier.check(
            "SELECT business_line FROM fact_sales "
            "WHERE fact_sales.business_line IN (1)",
            self.ctx,
            self.scope,
        )
        self.assertTrue(result.ok)

    async def test_accepts_multiple_values(self) -> None:
        result = await self.verifier.check(
            "SELECT business_line FROM fact_sales "
            "WHERE fact_sales.business_line IN (1, 2)",
            self.ctx,
            scope_for([1, 2]),
        )
        self.assertTrue(result.ok)

    async def test_rejects_partial_values(self) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.verifier.check(
                "SELECT business_line FROM fact_sales "
                "WHERE fact_sales.business_line IN (1)",
                self.ctx,
                scope_for([1, 2]),
            )
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_NOT_INJECTED")

    async def test_rejects_or_bypass(self) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.verifier.check(
                "SELECT business_line FROM fact_sales "
                "WHERE fact_sales.business_line IN (1) OR 1=1",
                self.ctx,
                self.scope,
            )
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_NOT_INJECTED")

    async def test_rejects_is_not_null(self) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.verifier.check(
                "SELECT business_line FROM fact_sales "
                "WHERE fact_sales.business_line IS NOT NULL",
                self.ctx,
                self.scope,
            )
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_NOT_INJECTED")

    async def test_empty_scope_is_denied(self) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.verifier.check(
                "SELECT business_line FROM fact_sales",
                self.ctx,
                scope_for([]),
            )
        self.assertEqual(raised.exception.symbol, "SQL_SCOPE_EMPTY")

    async def test_no_scope_is_allowed(self) -> None:
        result = await self.verifier.check(
            "SELECT business_line FROM fact_sales",
            self.ctx,
            None,
        )
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
