"""★ B4 ``QueryExecutor``（唯一执行入口）+ 七步流水线 + 大屏组装 单测。

对齐《设计方案 v2》§6.2 / §6.3.4 / §9.5、红线 6 与《实现要点清单》§5.8、§5.9。

断言的关键语义：
  * ``QueryExecutor`` 是唯一入口：护栏链必过、独立只读池取数、超时双保险、行数截断 + 审计；
  * ``run_sql``（内核 ``SqlRunner`` 路径）仍从 ``ctx.extra['_scope']`` 取权限，**无旁路**；
  * 七步流水线步骤表冻结、注册名与步骤一致；
  * ``DashboardSpecBuilder``：小结果内联 / 大结果走 s3、``materialize`` 签发预签名 URL。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from dba.modules.chatbi import CHATBI_STEPS, build_chatbi
from dba.modules.chatbi.guard import CompiledScope
from dba.modules.chatbi.spec_builder import DashboardSpecBuilder
from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError, TransientSqlError
from dba_runtime.events import InMemoryEmitter


# ═════════════════════════════════════════════════════════════════════════
# 测试替身
# ═════════════════════════════════════════════════════════════════════════
class FakeAudit:
    """``SqlAuditRepo`` 替身（只记录，不落库）。"""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.truncated: list[tuple[str, int, int]] = []

    async def write(self, **kw: Any) -> int:
        self.writes.append(kw)
        return len(self.writes)

    async def mark_truncated(self, trace_id: str, rows_returned: int, max_rows: int) -> int:
        self.truncated.append((trace_id, rows_returned, max_rows))
        return 1

    async def insert(self, row: dict[str, Any]) -> int:  # noqa: ARG002
        return 1

    async def query(self, flt: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ARG002
        return []


class FakeScopeRepo:
    """``row_scope_rule`` 仓储替身：默认无任何规则。"""

    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:  # noqa: ARG002
        return []

    async def rule_version(self, role_ids: list[int]) -> str | None:  # noqa: ARG002
        return None


class FakeMappingRepo:
    async def all_mappings(self) -> list[dict[str, Any]]:
        return []


class _Result:
    def __init__(self, keys: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self._keys = list(keys)
        self._rows = list(rows)

    def keys(self) -> list[str]:
        return list(self._keys)

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        return list(self._rows[:n])


class _Conn:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def exec_driver_sql(self, sql: str) -> _Result:
        self._pool.calls.append(sql)
        if not sql.upper().lstrip().startswith("EXPLAIN") and self._pool.delay > 0:
            await asyncio.sleep(self._pool.delay)
        return _Result(self._pool.keys, self._pool.rows)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakePool:
    """伪只读池：``acquire()`` 返回一个记录调用、可注入延迟的连接。"""

    def __init__(
        self,
        keys: tuple[str, ...] = ("id", "v"),
        rows: list[tuple[Any, ...]] | None = None,
        *,
        delay: float = 0.0,
    ) -> None:
        self.keys = keys
        self.rows = rows if rows is not None else [(1, "a"), (2, "b")]
        self.delay = delay
        self.calls: list[str] = []

    def acquire(self) -> _Acquire:
        return _Acquire(_Conn(self))


def make_ctx(**kw: object) -> RunContext:
    base: dict[str, object] = {"trace_id": "c" * 32, "span_id": "d" * 16, "module": "chatbi"}
    base.update(kw)
    return RunContext(**base)  # type: ignore[arg-type]


def _bundle(pool: FakePool, audit: FakeAudit, **kw: Any):  # type: ignore[no-untyped-def]
    return build_chatbi(
        audit=audit,
        scope_rule_repo=FakeScopeRepo(),
        field_mapping_repo=FakeMappingRepo(),
        readonly_pool=pool,
        allowed_tables={"t_order", "t_fact", "t_dim"},
        max_rows=100,
        timeout_s=5,
        **kw,
    )


# ═════════════════════════════════════════════════════════════════════════
# QueryExecutor（唯一执行入口）
# ═════════════════════════════════════════════════════════════════════════
async def test_execute_runs_guarded_query_and_writes_audit() -> None:
    pool = FakePool(keys=("id", "v"), rows=[(1, "a"), (2, "b")])
    audit = FakeAudit()
    b = _bundle(pool, audit)
    rows, meta = await b.executor.execute(
        "SELECT id, v FROM t_order", make_ctx(user_id=7), None, max_rows=100, timeout_s=5
    )
    assert rows == [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    assert meta["row_count"] == 2
    assert meta["truncated"] is False
    # 护栏链 rewrite 审计 + 执行 rewrite 审计
    assert sum(1 for w in audit.writes if w.get("decision") == "rewrite") >= 1
    # 真实取数前先经 dry-run 的 EXPLAIN（同一只读池）
    assert len([c for c in pool.calls if c.upper().startswith("EXPLAIN")]) == 1
    assert any(c.upper().lstrip().startswith("SELECT") for c in pool.calls)


async def test_execute_truncates_and_marks_audit() -> None:
    pool = FakePool(keys=("id",), rows=[(i,) for i in range(5)])
    audit = FakeAudit()
    b = _bundle(pool, audit)
    rows, meta = await b.executor.execute(
        "SELECT id FROM t_order", make_ctx(), None, max_rows=2, timeout_s=5
    )
    assert len(rows) == 2  # 截断到上限
    assert meta["truncated"] is True
    assert audit.truncated and audit.truncated[0][1] == 2  # mark_truncated 被调用


async def test_execute_denies_write_sql_and_writes_deny_audit() -> None:
    pool = FakePool()
    audit = FakeAudit()
    b = _bundle(pool, audit)
    with pytest.raises(SqlGuardError) as ei:
        await b.executor.execute("DELETE FROM t_order", make_ctx(), None)
    assert ei.value.symbol == "SQL_NOT_READONLY"
    assert any(w.get("decision") == "deny" for w in audit.writes)
    # 被拒 SQL 绝不触达真实连接（连 EXPLAIN 都没有）
    assert pool.calls == []


async def test_execute_timeout_is_transient() -> None:
    pool = FakePool(delay=0.5)
    audit = FakeAudit()
    b = _bundle(pool, audit)
    with pytest.raises(TransientSqlError):
        await b.executor.execute(
            "SELECT id FROM t_order", make_ctx(), None, max_rows=10, timeout_s=0.05
        )


async def test_run_sql_reads_scope_from_ctx_and_injects() -> None:
    # ★ 红线 6：即便经内核 SqlTool / SqlRunner 路径，也必须从 ctx.extra['_scope']
    #   取权限并注入——不存在绕过护栏的旁路。
    pool = FakePool(keys=("region",), rows=[("east",)])
    audit = FakeAudit()
    b = _bundle(pool, audit)
    scope = CompiledScope(per_table={"t_order": ("region", ["east"])})
    ctx = make_ctx().with_extra(_scope=scope)
    rows, meta = await b.executor.run_sql("SELECT region FROM t_order", ctx, max_rows=100)
    assert rows == [{"region": "east"}]
    assert meta["scope_injected"] is True
    executed = [c for c in pool.calls if c.upper().lstrip().startswith("SELECT")]
    assert executed and "region IN ('east')" in executed[0]


# ═════════════════════════════════════════════════════════════════════════
# 七步流水线（冻结步骤表 + 端到端）
# ═════════════════════════════════════════════════════════════════════════
def test_chatbi_steps_are_frozen_seven_step_order() -> None:
    assert [s.name for s in CHATBI_STEPS] == [
        "intent",
        "schema_link",
        "sql_gen",
        "sql_guard",
        "sql_exec",
        "visual",
        "narrator",
    ]
    sql_exec = next(s for s in CHATBI_STEPS if s.name == "sql_exec")
    assert sql_exec.on_timeout == "goto" and sql_exec.goto_step == "sql_gen"
    assert "rows" in sql_exec.produces


def test_registry_has_all_seven_agents() -> None:
    b = _bundle(FakePool(), FakeAudit())
    assert b.registry.names() == [
        "intent",
        "narrator",
        "schema_link",
        "sql_exec",
        "sql_gen",
        "sql_guard",
        "visual",
    ]


class _Choice:
    def __init__(self, task: str) -> None:
        self.task = task
        self.model = "stub-model"


class _LLMResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.finish_reason = "stop"


class StubRouter:
    """任务感知的 LLM 路由器替身（sql_gen 给 SQL；visual 给布局 JSON）。"""

    def __init__(self, sql: str, panels_json: str = '{"panels": []}') -> None:
        self._sql = sql
        self._panels = panels_json

    async def route(self, *, task: str, ctx: RunContext) -> _Choice:  # noqa: ARG002
        return _Choice(task)

    async def chat(
        self, messages: list[dict[str, Any]], choice: _Choice, ctx: RunContext
    ) -> _LLMResult:  # noqa: ARG002
        return _LLMResult(self._sql if choice.task == "sql_gen" else self._panels)

    async def stream(self, messages: list[dict[str, Any]], choice: _Choice, ctx: RunContext):  # type: ignore[no-untyped-def]  # noqa: ARG002
        for chunk in ("本次结果", "共 2 行，", "口径为支付口径。"):
            yield chunk


async def test_pipeline_end_to_end_success() -> None:
    pool = FakePool(keys=("region", "total"), rows=[("east", 88), ("west", 12)])
    audit = FakeAudit()
    router = StubRouter(
        "```sql\nSELECT region, SUM(amount) AS total FROM t_order GROUP BY region\n```"
    )
    b = _bundle(pool, audit, router=router)

    emitter = InMemoryEmitter(trace_id="e" * 32)
    result = await b.pipeline.execute({"question": "华东销售额"}, make_ctx(), emitter)

    assert result.status == "success"
    assert result.steps_run == [
        "intent",
        "schema_link",
        "sql_gen",
        "sql_guard",
        "sql_exec",
        "visual",
        "narrator",
    ]
    assert result.output["row_count"] == 2
    assert result.output["narration"] == "本次结果共 2 行，口径为支付口径。"

    names = emitter.names()
    assert "sql.executing" in names and "sql.executed" in names
    assert "dashboard.spec.delta" in names and "dashboard.spec.ready" in names
    assert names.count("narration.delta") == 3

    spec = result.output["dashboard_spec"]
    assert spec["schema_version"] == "1.0"
    assert spec["panels"]  # 兜底自动出图（visual 给的 panels 为空）


async def test_pipeline_denied_sql_fails_without_looping_forever() -> None:
    # 护栏拒绝（非越权）→ goto 回退受 max_retries 约束，最终失败而非死循环
    pool = FakePool()
    audit = FakeAudit()
    router = StubRouter("```sql\nDELETE FROM t_order\n```")  # 始终生成非法 SQL
    b = _bundle(pool, audit, router=router)
    result = await b.pipeline.execute({"question": "删库"}, make_ctx(), InMemoryEmitter())
    assert result.status == "failed"
    # 生成侧最多被回退 2 次，故 sql_gen 访问次数远小于 max_visit（无 goto 环）
    assert result.steps_run.count("sql_gen") <= 3
    assert pool.calls == []  # 非法 SQL 从不触达真实连接


# ═════════════════════════════════════════════════════════════════════════
# 大屏组装 DashboardSpecBuilder
# ═════════════════════════════════════════════════════════════════════════
def test_spec_builder_skill_path_inline() -> None:
    builder = DashboardSpecBuilder(inline_threshold=500)
    spec = builder.from_skill(
        {
            "panels": [
                {"panel_id": "p1", "kind": "chart", "chart": {"type": "bar"}, "title": "销量"}
            ]
        },
        {"rows": [{"id": 1, "v": "a"}], "question": "销量"},
    )
    assert spec.schema_version == "1.0"
    assert spec.title == "销量"
    assert spec.panels[0].panel_id == "p1"
    assert spec.panels[0].kind == "chart"
    assert spec.data_sources[0].mode == "inline"
    assert spec.data_sources[0].rows == [[1, "a"]]


def test_spec_builder_large_result_goes_s3() -> None:
    builder = DashboardSpecBuilder(inline_threshold=2)
    rows = [{"id": i} for i in range(3)]
    spec = builder.from_llm_json(
        '{"panels": [{"panel_id": "p1", "kind": "table", "title": "明细"}]}', {"rows": rows}
    )
    source = spec.data_sources[0]
    assert source.mode == "s3"
    assert source.object_key
    assert source.row_count == 3
    assert source.rows == []  # ★ 大结果绝不内联


def test_spec_builder_auto_panels_when_no_template() -> None:
    builder = DashboardSpecBuilder()
    spec = builder.from_skill({}, {"rows": [{"d": "2026-01-01", "n": 5}], "question": "趋势"})
    assert spec.panels
    assert spec.panels[0].kind in {"chart", "table"}


def test_spec_builder_llm_json_tolerates_fenced_block() -> None:
    builder = DashboardSpecBuilder()
    payload_json = '{"panels":[{"panel_id":"px","kind":"metric_card","title":"KPI"}]}'
    text = f"说明如下：\n```json\n{payload_json}\n```"
    spec = builder.from_llm_json(text, {"rows": [{"k": 1}]})
    assert spec.panels[0].panel_id == "px"
    assert spec.panels[0].kind == "metric_card"


class _Store:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []

    async def put(self, bucket: str, key: str, body: bytes, content_type: str) -> None:  # noqa: ARG002
        self.puts.append((bucket, key))

    async def presigned_get(self, bucket: str, key: str, *, expires_s: int) -> str:  # noqa: ARG002
        return f"https://minio.local/{bucket}/{key}"


async def test_materialize_uploads_and_signs_url() -> None:
    store = _Store()
    builder = DashboardSpecBuilder(object_store=store, inline_threshold=0)
    spec = builder.from_skill(
        {"panels": [{"panel_id": "p1", "kind": "table", "title": "T"}]}, {"rows": [{"id": 1}]}
    )
    assert spec.data_sources[0].mode == "s3"
    spec = await builder.materialize(
        spec, {"rows": [{"id": 1}], "columns": [{"name": "id", "type": "number"}]}
    )
    assert store.puts
    assert spec.data_sources[0].presigned_url is not None
    assert spec.data_sources[0].presigned_url.startswith("https://minio.local/")
