"""FR-T01：ChatBI 主链路特征测试。"""

from __future__ import annotations

import unittest

from dba.modules.chatbi.pipeline import build_chatbi_pipeline
from dba_runtime.agent import AgentOutput
from dba_runtime.errors import SqlGuardError, TransientSqlError
from dba_runtime.events import InMemoryEmitter

from tests.feature.support import build_registry, make_ctx, ok_handler


class ChatbiFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_normal_flow_runs_all_steps(self) -> None:
        pipeline = build_chatbi_pipeline(build_registry())
        result = await pipeline.execute({}, make_ctx(), InMemoryEmitter())
        self.assertEqual(result.status, "success")
        self.assertEqual(
            result.steps_run,
            ["intent", "schema_link", "sql_gen", "sql_guard", "sql_exec", "visual", "narrator"],
        )
        self.assertEqual(result.output["steps"], result.steps_run)

    async def test_sql_gen_retries_after_transient_error(self) -> None:
        calls = {"sql_gen": 0}

        async def sql_gen(payload, ctx):
            calls["sql_gen"] += 1
            if calls["sql_gen"] == 1:
                raise TransientSqlError("transient")
            return await ok_handler("sql_gen")(payload, ctx)

        pipeline = build_chatbi_pipeline(build_registry({"sql_gen": sql_gen}))
        result = await pipeline.execute({}, make_ctx(), InMemoryEmitter())
        self.assertEqual(result.status, "success")
        self.assertGreaterEqual(result.retries, 1)
        self.assertEqual(calls["sql_gen"], 2)
        self.assertEqual(result.steps_run.count("sql_gen"), 2)

    async def test_permission_denied_short_circuits(self) -> None:
        calls = {"sql_gen": 0}

        async def sql_gen(payload, ctx):
            calls["sql_gen"] += 1
            return await ok_handler("sql_gen")(payload, ctx)

        async def sql_guard(payload, ctx):
            raise SqlGuardError("denied", symbol="SQL_PERMISSION_DENIED")

        pipeline = build_chatbi_pipeline(
            build_registry({"sql_gen": sql_gen, "sql_guard": sql_guard})
        )
        result = await pipeline.execute({}, make_ctx(), InMemoryEmitter())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "SQL_PERMISSION_DENIED")
        self.assertEqual(calls["sql_gen"], 1)
        self.assertEqual(
            result.steps_run,
            ["intent", "schema_link", "sql_gen", "sql_guard"],
        )
        self.assertEqual(result.output["_last_failure"]["failed_step"], "sql_guard")
        self.assertEqual(
            result.output["_last_failure"]["error_code"],
            "SQL_PERMISSION_DENIED",
        )


if __name__ == "__main__":
    unittest.main()
