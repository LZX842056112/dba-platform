"""★ B6 端到端冒烟：提问 → 七步流水线 → 出图（不依赖外部密钥 / 真实 DB）。

对齐《设计方案 v2》§8.1 / §9.3 / §9.5。

为什么用替身而不是真实 LLM / MySQL：
  * 本机无 Docker、无外部 LLM 密钥；e2e 的价值在于**串起整条链路**（intent→…→narrator）
    并验证「提问 → 进度事件 → SQL 执行 → 大屏 spec」的数据流与事件协议，
    而不是验证模型质量或 DB 行为（那分别由 golden 套件与 MySQL 集成测试覆盖）。

断言：
  1. 七步全部执行且状态 success，产出 ``dashboard_spec``（≥1 个 panel）与 narration；
  2. 流式事件齐备：``sql.executing`` / ``sql.executed`` / ``dashboard.spec.delta`` /
     ``dashboard.spec.ready`` / ``narration.delta``；
  3. ``build_sse_frames`` 每条帧都写 ``id: {seq}``（★ P0-7：浏览器才会带 Last-Event-ID）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from dba.api.v1.stream import build_sse_frames
from dba.modules.chatbi import CHATBI_STEPS, build_chatbi
from dba_runtime.context import RunContext
from dba_runtime.events import InMemoryEmitter


# ═════════════════════════════════════════════════════════════════════════
# 替身
# ═════════════════════════════════════════════════════════════════════════
class FakeAudit:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def write(self, **kw: Any) -> int:
        self.writes.append(kw)
        return len(self.writes)

    async def mark_truncated(self, *a: Any, **kw: Any) -> int:
        _ = (a, kw)
        return 1


class FakeScopeRepo:
    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:
        # 角色 1 = 华东：行级权限注入 fact.region = 'east'
        if role_ids and 1 in role_ids:
            return [
                {
                    "enabled": 1,
                    "physical_table": "t_fact",
                    "scope_column": "region",
                    "value_type": "STATIC",
                    "value_json": '["east"]',
                }
            ]
        return []

    async def rule_version(self, role_ids: list[int]) -> str | None:
        _ = role_ids
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
        return _Result(self._pool.keys, self._pool.rows)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakePool:
    """伪只读池：记录被执行的 SQL，返回固定结果。"""

    def __init__(self) -> None:
        self.keys: tuple[str, ...] = ("region", "total")
        self.rows: list[tuple[Any, ...]] = [("east", 188)]
        self.calls: list[str] = []

    def acquire(self) -> _Acquire:
        return _Acquire(_Conn(self))


class _Choice:
    def __init__(self, task: str) -> None:
        self.task = task
        self.model = "e2e-stub"


class _LLMResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.finish_reason = "stop"


class StubRouter:
    """任务感知 LLM 替身（sql_gen 给 SQL；visual 给布局；narration 流式）。"""

    async def route(self, *, task: str, ctx: RunContext) -> _Choice:
        _ = ctx
        return _Choice(task)

    async def chat(
        self, messages: list[dict[str, Any]], choice: _Choice, ctx: RunContext
    ) -> _LLMResult:
        _ = (messages, ctx)
        if choice.task == "sql_gen":
            return _LLMResult("```sql\nSELECT region, SUM(amount) AS total FROM t_fact\n```")
        return _LLMResult('{"panels": []}')

    async def stream(self, messages: list[dict[str, Any]], choice: _Choice, ctx: RunContext) -> Any:
        _ = (messages, choice, ctx)
        for chunk in ("本期结果：", "华东 188。"):
            yield chunk


def _ctx() -> RunContext:
    return RunContext(trace_id="e" * 32, span_id="0" * 16, module="chatbi", user_id=7)


# ═════════════════════════════════════════════════════════════════════════
# e2e
# ═════════════════════════════════════════════════════════════════════════
async def _run() -> tuple[Any, Any, FakePool]:
    pool = FakePool()
    bundle = build_chatbi(
        audit=FakeAudit(),
        scope_rule_repo=FakeScopeRepo(),
        field_mapping_repo=FakeMappingRepo(),
        readonly_pool=pool,
        router=StubRouter(),
        allowed_tables={"t_fact"},
        max_rows=100,
        timeout_s=5,
    )
    emitter = InMemoryEmitter(trace_id="e" * 32)
    result = await bundle.pipeline.execute(
        {"question": "华东销售额", "_role_ids": [1]}, _ctx(), emitter
    )
    return result, emitter, pool


async def test_e2e_question_to_dashboard() -> None:
    result, emitter, pool = await _run()

    # 1) 七步全部执行，状态成功
    assert result.status == "success"
    assert result.steps_run == [s.name for s in CHATBI_STEPS]

    # 2) 产出大屏 spec + narration
    spec = result.output["dashboard_spec"]
    assert spec["schema_version"] == "1.0"
    assert spec["panels"], "应至少产出一个面板"
    assert spec["layout"]["cols"] == 12
    assert result.output["narration"] == "本期结果：华东 188。"

    # 3) 行级权限被注入，且真实执行的是**改写后**的 SQL
    executed = [c for c in pool.calls if c.upper().lstrip().startswith("SELECT")]
    assert executed and "region IN ('east')" in executed[0]

    # 4) 流式事件齐备（§9.3 协议）
    names = emitter.names()
    for expected in (
        "sql.executing",
        "sql.executed",
        "dashboard.spec.delta",
        "dashboard.spec.ready",
        "narration.delta",
    ):
        assert expected in names, f"缺少事件：{expected}"


async def test_e2e_sse_frames_carry_id_seq() -> None:
    # ★ P0-7：服务端必须写 ``id: {seq}``，浏览器重连才会带 Last-Event-ID
    _, emitter, _ = await _run()
    frames = build_sse_frames(list(emitter.events))
    assert frames and len(frames) == len(emitter.events)
    for frame, event in zip(frames, emitter.events, strict=True):
        assert frame.startswith(f"id: {event.seq}\n")
        assert f"event: {event.event}\n" in frame
        assert frame.endswith("\n\n")


def test_e2e_runs_under_asyncio() -> None:
    # 显式跑一次（pytest-asyncio 亦可发现上面两个 async 用例；这里保证独立可执行）
    asyncio.run(_run())
