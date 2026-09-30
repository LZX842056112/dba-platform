"""④ ``SqlGuardAgent``：调用护栏链；回退重生成通过时发 ``sql.retry.resolved``。

对齐《设计方案 v2》§6.2 / §6.3 与《实现要点清单》§5.9（3.5）。

★ 失败即抛错（由 ``SqlGuardChain`` 抛出内核 ``SqlGuardError``），由 Pipeline 回退到
  ``sql_gen``；★ 越权类（``SQL_PERMISSION_DENIED``）由 Pipeline 短路、**不重试**。

★ 重试识别：Pipeline 在失败步骤上触发 ``error`` 钩子时会把失败信息记进数据包
  ``_last_failure``（见 ``pipeline._record_failure``）；本 Agent 在通过护栏且
  ``_last_failure`` 指向护栏/执行阶段时，发 ``sql.retry.resolved``——
  这是「自愈回退成功」的可观测证据（§9.3 事件生产者表指定本 Agent 为唯一生产者）。
"""

from __future__ import annotations

from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

from ..guard.base import SqlGuardChain

__all__ = ["SqlGuardAgent"]


class SqlGuardAgent:
    """SQL 校验（无状态）。"""

    name = "sql_guard"

    def __init__(self, chain: SqlGuardChain) -> None:
        self._chain = chain

    @traced("step.sql_guard", kind="guard")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        sql = str(payload.get("sql") or "")
        scope = payload.get("_scope")
        result = await self._chain.validate(sql, ctx, scope)
        final_sql = result.rewritten_sql or sql

        last = payload.get("_last_failure")
        if (
            isinstance(last, dict)
            and last.get("failed_step") in ("sql_guard", "sql_exec")
            and ctx.emitter is not None
        ):
            await ctx.emitter.emit(
                "sql.retry.resolved",
                {
                    "sql": final_sql,
                    "prev_error_code": last.get("error_code"),
                    "scope_injected": result.scope_injected,
                },
            )
        return AgentOutput(
            data={"sql": final_sql, "scope_injected": result.scope_injected},
            confidence=1.0 if result.scope_injected or scope is None else 0.6,
        )
