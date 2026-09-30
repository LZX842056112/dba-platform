"""只读 SQL 执行工具（内核 L2）。

对齐《设计方案 v2》§6.1.3 与《实现要点清单》§2.2。

★ 重要：本工具**不是**真实执行入口。全平台唯一真实 SQL 执行入口是 L3 的
``QueryExecutor.execute``（红线 6）。本工具只做「超时 / 行数上限」两件防御性封装，
真正的连接、护栏、审计由注入的 runner（B4 提供）负责，因此内核不 import 任何 DB 驱动。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, TypedDict, runtime_checkable

from dba_runtime.context import RunContext
from dba_runtime.tools.base import ToolResult

__all__ = ["SqlRunner", "SqlTool", "ExecMeta"]

#: 默认行数上限与超时
DEFAULT_MAX_ROWS = 5000
DEFAULT_TIMEOUT_S = 30.0


class ExecMeta(TypedDict, total=False):
    """★ U8：``ExecMeta{row_count, exec_ms, scope_injected, truncated}``。"""

    row_count: int
    exec_ms: int
    scope_injected: bool
    truncated: bool


@runtime_checkable
class SqlRunner(Protocol):
    """SQL 执行后端 Protocol（由 B4 ``QueryExecutor`` 实现）。

    返回 ``(rows, meta)``：``rows`` 为行字典列表；``meta`` 至少含
    ``row_count`` / ``exec_ms`` / ``scope_injected`` / ``truncated``（★ U8）。
    """

    async def run_sql(
        self,
        sql: str,
        ctx: RunContext,
        *,
        max_rows: int,
        timeout_s: float,
    ) -> tuple[list[dict[str, Any]], ExecMeta]: ...


class SqlTool:
    """只读 SQL 工具。``name`` 固定为 ``"sql_exec"``。"""

    name = "sql_exec"

    def __init__(
        self,
        runner: SqlRunner,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._runner = runner
        self._max_rows = max_rows
        self._timeout_s = timeout_s

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        """执行只读 SQL。

        ``args``：``{"sql": str, "max_rows"?: int, "timeout_s"?: float}``。
        """
        sql = str(args.get("sql", "")).strip()
        if not sql:
            return ToolResult(ok=False, error="SQL_EMPTY", data={})

        max_rows = int(args.get("max_rows", self._max_rows))
        timeout_s = float(args.get("timeout_s", self._timeout_s))

        try:
            async with asyncio.timeout(timeout_s):
                rows, meta = await self._runner.run_sql(
                    sql, ctx, max_rows=max_rows, timeout_s=timeout_s
                )
        except TimeoutError:
            return ToolResult(ok=False, error="SQL_TIMEOUT", data={"sql": sql})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=type(exc).__name__, data={"message": str(exc)})

        truncated = bool(meta.get("truncated", False)) or len(rows) > max_rows
        return ToolResult(
            ok=True,
            data={
                "rows": rows[:max_rows],
                "row_count": len(rows),
                "exec_ms": int(meta.get("exec_ms", 0)),
                "scope_injected": bool(meta.get("scope_injected", False)),
                "truncated": truncated,
            },
        )
