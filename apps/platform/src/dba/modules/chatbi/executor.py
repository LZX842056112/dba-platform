"""``QueryExecutor``：全平台**唯一**允许执行真实 SQL 的入口（★ v2 新增）。

对齐《设计方案 v2》§6.3.4 / §6.2 与《实现要点清单》§5.8、红线 6。

★ 为什么必须是唯一入口（照抄 v1 会怎样错）
------------------------------------------
v1 的护栏只在对话主链路上被调用；WS 面板刷新、导出这两条路径同样会执行 SQL，
却没有任何强制约束。只要存在一条能直接拿连接的路径，AST 注入就只是主链路上的装饰品。
v2 用一个入口把这件事收口——**没有旁路**，行级权限的效力才成立。

★ ``execute`` 的四条硬约束（写在唯一入口里，任何调用方都绕不过去）
----------------------------------------------------------------
1) 必过 ``SqlGuardChain``（只读白名单 → 方言 → 行级注入 → 覆盖性断言 → LIMIT + 超时提示）
2) 连接来自**独立只读池**：``read_only=True``、关闭多语句、不共享 ORM 会话
3) 超时**双保险**：SQL 侧 ``MAX_EXECUTION_TIME`` 提示 + 应用侧 ``asyncio.timeout``
4) 行数上限：超限**截断并置** ``truncated=True``；被截断时同时写 ``sql_audit``

★ 已知边界（诚实标注）：内核 ``Pipeline`` 对 ``on_error="goto"`` 的步骤只在
   ``SQL_PERMISSION_DENIED`` 上短路；因此执行期**非瞬时**错误最多仍会触发 1 次
   ``goto``（受 ``step.max_retries`` 约束，不会无限放大）。元数据类错误已被
   ⑤ dry-run 在护栏阶段拦下，故落到执行期的多为瞬时错误。
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from weakref import WeakKeyDictionary

from dba_runtime.context import RunContext
from dba_runtime.errors import DbaError, TransientSqlError, classify
from dba_runtime.tools.sql_tool import ExecMeta

from dba.storage.protocols import SqlAuditRepo

from .guard.base import GuardResult, SqlGuardChain, sql_guard_error
from .guard.dry_run import ReadOnlyConnection
from .guard.row_scope import CompiledScope, ScopePredicate

__all__ = ["QueryExecutor"]

DEFAULT_MAX_ROWS = 5000
DEFAULT_TIMEOUT_S = 30.0


def query_context(ctx: RunContext, scope_hash: str | None) -> RunContext:
    return replace(ctx, scope_hash=scope_hash) if scope_hash else ctx


@dataclass(frozen=True)
class _PreparedQuery:
    original: str
    sql: str
    scope_injected: bool


@dataclass(frozen=True)
class _PendingPrimary:
    query: _PreparedQuery
    ctx: RunContext
    scope: CompiledScope | None


class QueryExecutor:
    """唯一真实 SQL 执行入口（同时实现内核 ``SqlRunner``）。"""

    def __init__(self, chain: SqlGuardChain, pool: Any, audit: SqlAuditRepo) -> None:
        self._chain = chain
        self._conn = ReadOnlyConnection(pool)
        self._audit = audit
        self._pending: WeakKeyDictionary[asyncio.Task[Any], _PendingPrimary] = WeakKeyDictionary()

    async def _prepare(
        self, sql: str, ctx: RunContext, scope: ScopePredicate | None
    ) -> _PreparedQuery:
        result = await self._chain.validate(sql, ctx, scope)
        return _PreparedQuery(sql, result.rewritten_sql or sql, result.scope_injected)

    async def prepare_primary(
        self, sql: str, ctx: RunContext, scope: CompiledScope | None
    ) -> GuardResult:
        """校验本次流水线的主查询。重新校验前先清除旧结果。"""
        task = asyncio.current_task()
        assert task is not None
        self._pending.pop(task, None)
        snapshot = deepcopy(scope)
        query = await self._prepare(sql, ctx, snapshot)
        self._pending[task] = _PendingPrimary(query, ctx, snapshot)
        return GuardResult(ok=True, rewritten_sql=query.sql, scope_injected=query.scope_injected)

    async def execute_primary(
        self, sql: str, ctx: RunContext, scope: CompiledScope | None, *, max_rows: int
    ) -> tuple[list[dict[str, Any]], ExecMeta]:
        """一次性取出本任务的主查询。执行失败后须重新校验。"""
        task = asyncio.current_task()
        assert task is not None
        pending = self._pending.pop(task, None)
        if pending is None or pending.query.sql != sql or pending.ctx != ctx or pending.scope != scope:
            raise sql_guard_error("主查询的校验结果已失效", "SQL_GUARD_BLOCKED", stage="sql_exec")
        return await self._fetch(pending.query, ctx, max_rows, DEFAULT_TIMEOUT_S)

    async def execute(
        self,
        sql: str,
        ctx: RunContext,
        scope: ScopePredicate | None,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> tuple[list[dict[str, Any]], ExecMeta]:
        """执行只读 SQL：先过护栏，再用独立只读连接取数，最后写审计。"""
        query = await self._prepare(sql, ctx, scope)
        return await self._fetch(query, ctx, max_rows, timeout_s)

    async def _fetch(
        self, query: _PreparedQuery, ctx: RunContext, max_rows: int, timeout_s: float
    ) -> tuple[list[dict[str, Any]], ExecMeta]:
        # ②③ 独立只读连接 + 应用侧超时（连接级 MAX_EXECUTION_TIME 由只读池设置）
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                rows, truncated = await self._conn.fetch(query.sql, max_rows=max_rows)
        except TimeoutError as exc:
            raise TransientSqlError(
                f"SQL 执行超时（{timeout_s}s）",
                detail={"trace_id": ctx.trace_id, "timeout_s": timeout_s},
            ) from exc
        except DbaError:
            raise
        except Exception as exc:  # noqa: BLE001
            if classify(exc)[0] == "SQL_EXEC_TRANSIENT":
                raise TransientSqlError(str(exc), detail={"trace_id": ctx.trace_id}) from exc
            raise
        exec_ms = int((time.perf_counter() - started) * 1000)

        meta: ExecMeta = {
            "row_count": len(rows),
            "exec_ms": exec_ms,
            "scope_injected": query.scope_injected,
            "truncated": truncated,
        }

        # ④ 审计（含 scope_hash，便于事后抽查「注入了权限的样本」）
        await self._audit.write(
            trace_id=ctx.trace_id,
            sql_text=query.original,
            rewritten_sql=query.sql,
            decision="rewrite",
            scope_injected=query.scope_injected,
            scope_hash=ctx.scope_hash,
            rows_returned=len(rows),
            exec_ms=exec_ms,
            user_id=ctx.user_id,
            biz_line_id=ctx.biz_line_id,
        )
        if truncated:
            await self._audit.mark_truncated(ctx.trace_id, len(rows), max_rows)
        return rows, meta

    # ── 内核 SqlRunner Protocol 兼容 ────────────────────────────────
    async def run_sql(
        self,
        sql: str,
        ctx: RunContext,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> tuple[list[dict[str, Any]], ExecMeta]:
        """内核 ``SqlTool`` 调用本方法。★ 仍走同一入口，不存在旁路。

        权限子域从 ``ctx.extra["_scope"]`` 取（Pipeline 在 schema_link 阶段写入），
        因此即便经 ``SqlTool`` 调用也**不会**绕过行级权限注入。
        """
        scope = ctx.extra.get("_scope")
        return await self.execute(
            sql,
            ctx,
            scope if scope is not None else None,
            max_rows=max_rows,
            timeout_s=timeout_s,
        )
