"""FR-T03：SQL 护栏拒绝路径特征测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dba.modules.chatbi.guard import (
    DialectGuard,
    LimitGuard,
    ReadonlyGuard,
    SqlGuardChain,
    SqlGuardError,
)
from dba.modules.chatbi.guard.dry_run import (
    DryRunGuard,
    ReadOnlyPermissionError,
)

from tests.feature.support import (
    FakeAuditRepo,
    FakeReadOnlyConnection,
    make_ctx,
)


class ReadonlyGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.guard = ReadonlyGuard(
            allowed_tables={"fact_sales", "dim_region"},
            extra_functions={"sum"},
        )

    async def assert_symbol(self, sql: str, symbol: str) -> None:
        with self.assertRaises(SqlGuardError) as raised:
            await self.guard.check(sql, self.ctx, None)
        self.assertEqual(raised.exception.symbol, symbol)
        self.assertEqual(raised.exception.stage, "readonly")

    async def test_allows_readonly_query(self) -> None:
        result = await self.guard.check(
            "SELECT business_line, SUM(amount) FROM fact_sales GROUP BY business_line",
            self.ctx,
            None,
        )
        self.assertTrue(result.ok)

    async def test_rejects_multi_statement(self) -> None:
        await self.assert_symbol("SELECT 1; DROP TABLE fact_sales", "SQL_MULTI_STATEMENT")

    async def test_rejects_write_statement(self) -> None:
        await self.assert_symbol("UPDATE fact_sales SET amount = 0", "SQL_NOT_READONLY")

    async def test_rejects_system_schema(self) -> None:
        await self.assert_symbol("SELECT * FROM information_schema.tables", "SQL_TABLE_NOT_ALLOWED")

    async def test_rejects_unknown_table(self) -> None:
        await self.assert_symbol("SELECT * FROM unknown_table", "SQL_TABLE_NOT_ALLOWED")

    async def test_rejects_into_outfile(self) -> None:
        await self.assert_symbol(
            "SELECT * FROM fact_sales INTO OUTFILE '/tmp/x'",
            "SQL_DANGEROUS_CONSTRUCT",
        )

    async def test_rejects_lock_read(self) -> None:
        await self.assert_symbol(
            "SELECT * FROM fact_sales FOR UPDATE",
            "SQL_DANGEROUS_CONSTRUCT",
        )

    async def test_rejects_dangerous_function(self) -> None:
        await self.assert_symbol("SELECT sleep(10)", "SQL_DANGEROUS_FUNCTION")

    async def test_rejects_unknown_function(self) -> None:
        await self.assert_symbol("SELECT my_func(1)", "SQL_FUNCTION_NOT_ALLOWED")

    async def test_rejects_parse_error(self) -> None:
        await self.assert_symbol("SELECT FROM", "SQL_PARSE_ERROR")


class DialectGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()

    async def test_allows_valid_mysql(self) -> None:
        result = await DialectGuard().check("SELECT 1", self.ctx, None)
        self.assertTrue(result.ok)

    async def test_maps_render_failure(self) -> None:
        with patch(
            "dba.modules.chatbi.guard.dialect.sqlglot.transpile",
            side_effect=RuntimeError("unsupported"),
        ):
            with self.assertRaises(SqlGuardError) as raised:
                await DialectGuard().check("SELECT 1", self.ctx, None)
        self.assertEqual(raised.exception.symbol, "SQL_DIALECT_UNSUPPORTED")


class LimitGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.guard = LimitGuard(max_rows=5000, timeout_s=30)

    async def test_adds_limit_and_timeout(self) -> None:
        result = await self.guard.check(
            "SELECT business_line, SUM(amount) FROM fact_sales GROUP BY business_line",
            self.ctx,
            None,
        )
        self.assertIn("LIMIT 5000", result.rewritten_sql)
        self.assertIn("MAX_EXECUTION_TIME(30000)", result.rewritten_sql)

    async def test_caps_large_limit(self) -> None:
        result = await self.guard.check(
            "SELECT business_line FROM fact_sales LIMIT 999999",
            self.ctx,
            None,
        )
        self.assertIn("LIMIT 5000", result.rewritten_sql)

    async def test_keeps_smaller_limit(self) -> None:
        result = await self.guard.check(
            "SELECT business_line FROM fact_sales LIMIT 10",
            self.ctx,
            None,
        )
        self.assertIn("LIMIT 10", result.rewritten_sql)


class DryRunGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()

    async def test_allows_successful_explain(self) -> None:
        result = await DryRunGuard(FakeReadOnlyConnection()).check("SELECT 1", self.ctx, None)
        self.assertTrue(result.ok)

    async def test_maps_permission_error(self) -> None:
        guard = DryRunGuard(FakeReadOnlyConnection(ReadOnlyPermissionError("denied")))
        with self.assertRaises(SqlGuardError) as raised:
            await guard.check("SELECT 1", self.ctx, None)
        self.assertEqual(raised.exception.symbol, "SQL_PERMISSION_DENIED")

    async def test_maps_other_error(self) -> None:
        guard = DryRunGuard(FakeReadOnlyConnection(RuntimeError("boom")))
        with self.assertRaises(SqlGuardError) as raised:
            await guard.check("SELECT 1", self.ctx, None)
        self.assertEqual(raised.exception.symbol, "SQL_DRY_RUN_FAILED")


class SqlGuardChainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = make_ctx()
        self.audit = FakeAuditRepo()
        self.chain = SqlGuardChain(
            [
                ReadonlyGuard(allowed_tables={"fact_sales"}),
                DialectGuard(),
                LimitGuard(max_rows=5000, timeout_s=30),
                DryRunGuard(FakeReadOnlyConnection()),
            ],
            self.audit,
        )

    async def test_writes_deny_audit(self) -> None:
        with self.assertRaises(SqlGuardError):
            await self.chain.validate("UPDATE fact_sales SET amount = 0", self.ctx, None)
        self.assertEqual(self.audit.records[-1]["decision"], "deny")
        self.assertEqual(self.audit.records[-1]["guard_stage"], "readonly")
        self.assertEqual(self.audit.records[-1]["code"], "SQL_NOT_READONLY")

    async def test_writes_rewrite_audit(self) -> None:
        result = await self.chain.validate(
            "SELECT business_line FROM fact_sales",
            self.ctx,
            None,
        )
        self.assertTrue(result.ok)
        self.assertIn("LIMIT 5000", result.rewritten_sql)
        self.assertEqual(self.audit.records[-1]["decision"], "rewrite")
        self.assertEqual(self.audit.records[-1]["sql_text"], "SELECT business_line FROM fact_sales")


if __name__ == "__main__":
    unittest.main()
