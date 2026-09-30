"""``golden`` 套件：分层题集的 Execution Accuracy / 口径命中率 / 延迟（§12.3）。

运行方式（真实 MySQL）
----------------------
逐题把 ``candidate_sql``（**录播的模型输出**）经一个任务感知的 ``StubRouter`` 作为
``sql_gen`` 的输出喂给**完整七步流水线**；再把 ``gold_sql`` 直接经 ``QueryExecutor``
执行。两者结果按 §12.3 逐值比较得 EX。延迟取「首个 ``dashboard.spec.delta``」与
「``run.finished``（末事件）」的墙钟耗时。

★ 诚实边界（必须在报告里如实标注）
  1. 本套件用**录播候选 SQL**，EX 衡量的是「护栏注入 + 执行 + 比较」链路的正确性，
     **不是模型准确率**；真实 LLM 模式需外部密钥，本机未验证。
  2. 成本字段为 0（无真实 LLM 计费），成本门槛为**平凡通过**。
  3. 需要可用的真实 MySQL；未设置 DSN 时本套件 skip（不假装通过）。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from dba_runtime.context import RunContext
from dba_runtime.pipeline import PipelineResult

from dba.modules.chatbi import build_chatbi
from dba.modules.chatbi.guard.row_scope import CompiledScope
from dba.storage.mysql.engine import ReadOnlyPool, build_engine

from .compare import rows_equal
from .models import CaseOutcome, GoldenCase, SuiteResult

__all__ = ["run_golden_suite", "EVAL_TABLES"]

FACT = "eval_fact_sales"
CHANNEL = "eval_dim_channel"
EVAL_TABLES = {FACT, CHANNEL}

_ROLE_IDS: dict[str, int] = {"east": 1, "global": 2, "empty": 3}

#: 评测数据集（金标事实：region ∈ {east,west,north}）
_FACT_ROWS: list[tuple[int, str, str, str, float]] = [
    (1, "east", "online", "2026-08-05", 1000.00),
    (2, "east", "offline", "2026-08-20", 500.00),
    (3, "west", "online", "2026-08-11", 700.00),
    (4, "north", "online", "2026-08-25", 300.00),
    (5, "east", "online", "2026-09-03", 1200.00),
    (6, "east", "offline", "2026-09-15", 400.00),
    (7, "west", "online", "2026-09-08", 900.00),
    (8, "north", "offline", "2026-09-19", 250.00),
]
_CHANNEL_ROWS: list[tuple[int, str]] = [(1, "online"), (2, "offline")]


class _MemoryAudit:
    """内存审计（评测无需落 ``sql_audit`` 表，避免对迁移产生额外耦合）。"""

    def __init__(self) -> None:
        self.writes: int = 0

    async def write(self, **kwargs: Any) -> int:
        _ = kwargs
        self.writes += 1
        return self.writes

    async def mark_truncated(self, *args: Any, **kwargs: Any) -> int:
        _ = (args, kwargs)
        return 1


class _ScopeRepo:
    """角色 → 行级规则（``region`` 直接位于事实表，无需 join 路径）。"""

    _VALUES: dict[int, list[str]] = {1: ["east"], 2: ["east", "west", "north"], 3: []}

    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:
        for rid in role_ids:
            if rid in self._VALUES:
                return [
                    {
                        "enabled": 1,
                        "physical_table": FACT,
                        "scope_column": "region",
                        "value_type": "STATIC",
                        "value_json": json.dumps(self._VALUES[rid]),
                    }
                ]
        return []

    async def rule_version(self, role_ids: list[int]) -> str | None:
        _ = role_ids
        return "eval-v1"


class _MappingRepo:
    async def all_mappings(self) -> list[dict[str, Any]]:
        return []


class _Choice:
    def __init__(self, task: str) -> None:
        self.task = task
        self.model = "eval-stub"


class _LLMResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.finish_reason = "stop"


class _StubRouter:
    """任务感知的 LLM 替身：``sql_gen`` 回放录播 SQL，其余给确定性输出。"""

    def __init__(self, candidate_sql: str) -> None:
        self._sql = candidate_sql

    async def route(self, *, task: str, ctx: RunContext) -> _Choice:
        _ = ctx
        return _Choice(task)

    async def chat(
        self, messages: list[dict[str, Any]], choice: _Choice, ctx: RunContext
    ) -> _LLMResult:
        _ = (messages, ctx)
        if choice.task == "sql_gen":
            return _LLMResult(self._sql)
        if choice.task == "visual_layout":
            return _LLMResult('{"panels": []}')  # 触发兜底自动出图
        return _LLMResult("")

    async def stream(
        self, messages: list[dict[str, Any]], choice: _Choice, ctx: RunContext
    ) -> AsyncIterator[str]:
        _ = (messages, choice, ctx)
        for chunk in ("本期结果已就绪，", "口径为 eval 演示口径。"):
            yield chunk


class _TimingEmitter:
    """记录每个事件相对起点的毫秒偏移（用于首图 / 端到端 P90）。"""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.marks: list[tuple[str, float]] = []
        self._seq = 0
        self._t0 = time.perf_counter()

    async def emit(self, event: Any, data: dict[str, Any]) -> None:
        _ = data
        self._seq += 1
        self.marks.append((str(event), (time.perf_counter() - self._t0) * 1000.0))

    def names(self) -> list[str]:
        return [name for name, _ in self.marks]

    def first_offset(self, event: str) -> float | None:
        for name, offset in self.marks:
            if name == event:
                return offset
        return None

    def last_offset(self) -> float:
        return self.marks[-1][1] if self.marks else 0.0


async def _bootstrap(engine: Any) -> None:
    """幂等建立评测表与数据（评测自持数据集，不依赖业务表）。"""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {FACT}")
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {CHANNEL}")
        await conn.exec_driver_sql(
            f"CREATE TABLE {CHANNEL} (id INT PRIMARY KEY, channel VARCHAR(16))"
        )
        await conn.exec_driver_sql(
            f"CREATE TABLE {FACT} ("
            "id INT PRIMARY KEY, region VARCHAR(16), channel VARCHAR(16), "
            "dt DATE, amount DECIMAL(18,2))"
        )
        await conn.exec_driver_sql(
            f"INSERT INTO {CHANNEL} (id, channel) VALUES "
            + ", ".join(f"({i}, '{c}')" for i, c in _CHANNEL_ROWS)
        )
        await conn.exec_driver_sql(
            f"INSERT INTO {FACT} (id, region, channel, dt, amount) VALUES "
            + ", ".join(f"({i}, '{r}', '{c}', '{d}', {a})" for i, r, c, d, a in _FACT_ROWS)
        )


def _ctx(trace: str) -> RunContext:
    return RunContext(trace_id=trace, span_id="0" * 16, module="chatbi", user_id=1, biz_line_id=1)


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(0.9 * (len(ordered) - 1))))
    return ordered[idx]


def _build_bundle(pool: ReadOnlyPool, router: _StubRouter) -> Any:
    return build_chatbi(
        audit=_MemoryAudit(),
        scope_rule_repo=_ScopeRepo(),
        field_mapping_repo=_MappingRepo(),
        readonly_pool=pool,
        router=router,
        allowed_tables=set(EVAL_TABLES),
        max_rows=5000,
        timeout_s=10,
    )


async def _run_one(case: GoldenCase, pool: ReadOnlyPool, trace: str) -> tuple[CaseOutcome, float]:
    role_id = _ROLE_IDS.get(case.role, 1)
    ctx = _ctx(trace)
    bundle = _build_bundle(pool, _StubRouter(case.candidate_sql))
    scope: CompiledScope | None = await bundle.scope_compiler.compile(role_ids=[role_id])

    emitter = _TimingEmitter(trace)
    result: PipelineResult = await bundle.pipeline.execute(
        {"question": case.question, "_role_ids": [role_id]}, ctx, emitter
    )
    first_panel = emitter.first_offset("dashboard.spec.delta")
    if first_panel is None:
        first_panel = emitter.last_offset()
    e2e = emitter.last_offset()

    if case.expects_denied:
        expected_code = case.expected_code
        failure = result.output.get("_last_failure") or {}
        code = str(failure.get("error_code") or "")
        ok = result.status == "failed" and (expected_code is None or code == expected_code)
        return CaseOutcome(case.id, ok, f"status={result.status} code={code or '∅'}", int(e2e)), (
            first_panel
        )

    candidate_rows: list[dict[str, Any]] = list(result.output.get("rows") or [])
    gold_rows, _meta = await bundle.executor.execute(case.gold_sql, ctx, scope)
    ex_ok = rows_equal(gold_rows, candidate_rows)
    caliber_ok = set(case.candidate_metric_codes) == set(case.metric_codes)
    ok = result.status == "success" and ex_ok
    detail = (
        f"status={result.status} ex={'ok' if ex_ok else 'MISMATCH'} "
        f"caliber={'ok' if caliber_ok else 'miss'} rows={len(candidate_rows)}"
    )
    return CaseOutcome(case.id, ok, detail, int(e2e)), first_panel


async def run_golden_suite(*, dsn: str, cases: list[GoldenCase]) -> SuiteResult:
    """在真实 MySQL 上运行 golden 套件。"""
    engine = build_engine(dsn)
    pool = ReadOnlyPool(dsn, timeout_s=10)
    await _bootstrap(engine)
    outcomes: list[CaseOutcome] = []
    first_panels: list[float] = []
    e2es: list[float] = []
    cat_total: dict[str, int] = {}
    cat_pass: dict[str, int] = {}
    caliber_total = 0
    caliber_hit = 0
    try:
        for case in cases:
            trace = f"golden{abs(hash(case.id)) % (16**10):010x}"
            outcome, first_panel = await _run_one(case, pool, trace)
            outcomes.append(outcome)
            first_panels.append(first_panel)
            e2es.append(float(outcome.latency_ms))
            cat_total[case.category] = cat_total.get(case.category, 0) + 1
            cat_pass[case.category] = cat_pass.get(case.category, 0) + int(outcome.passed)
            if not case.expects_denied:
                caliber_total += 1
                caliber_hit += int(set(case.candidate_metric_codes) == set(case.metric_codes))
    finally:
        await pool.aclose()
        await engine.dispose()

    result = SuiteResult(name="golden", outcomes=outcomes)
    result.metrics["ex_overall"] = result.pass_rate
    for category, total in sorted(cat_total.items()):
        result.metrics[f"ex_{category}"] = cat_pass[category] / total if total else 0.0
    result.metrics["caliber_hit_rate"] = caliber_hit / caliber_total if caliber_total else 0.0
    result.metrics["first_panel_p90_ms"] = _p90(first_panels)
    result.metrics["e2e_p90_ms"] = _p90(e2es)
    result.metrics["cost_per_question_micro_usd"] = 0.0  # 无真实 LLM，见模块 docstring
    return result
