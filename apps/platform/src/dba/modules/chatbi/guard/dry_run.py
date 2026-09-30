"""⑤ dry-run 护栏：只读账号预执行 ``EXPLAIN``，捕获元数据类错误。

对齐《设计方案 v2》§6.3.2 ⑤ / §6.3.4 与《实现要点清单》§5.8。

★ v2 的能力边界必须写清楚（否则会被误当成安全防线）
---------------------------------------------------
``EXPLAIN`` 只覆盖**元数据类**错误（列/表不存在、语法、对象权限），
覆盖不了**运行时**错误（除零、锁等待超时、慢查询被 kill）。
因此它**不能替代** ``sql_exec`` 的超时与错误分类，两者是互补关系而非替代关系。

★ ``SQL_PERMISSION_DENIED`` 的语义澄清：这**不是**「数据库层的行级兜底」，
而是「连接账号 / 对象权限配置错误」。共享只读账号自身不带 per-user 行级隔离，
真正的行级防线只有 ③ 的 AST 注入。Pipeline 对该 code **短路不重试**。

实现说明：``ReadOnlyConnection`` 与 ``ReadOnlyPermissionError`` 定义在本模块，
由 ``executor.py`` 复用，从而避免 guard 子包对父级模块产生相对 import。
"""

from __future__ import annotations

from typing import Any

from dba_runtime.context import RunContext

from .base import GuardResult, sql_guard_error

__all__ = ["DryRunGuard", "ReadOnlyConnection", "ReadOnlyPermissionError", "is_permission_error"]

#: MySQL 权限类错误码（访问被拒 / 命令被拒）
_PERMISSION_ERRNOS = frozenset({1044, 1045, 1142, 1143, 1227})


class ReadOnlyPermissionError(Exception):
    """只读账号的连接/对象权限配置错误（由 ``ReadOnlyConnection.explain`` 抛出）。"""


def is_permission_error(exc: BaseException) -> bool:
    """判断异常是否为 MySQL 权限类错误（duck-typing，不 import 具体驱动）。"""
    origin = getattr(exc, "orig", exc)
    args = getattr(origin, "args", ())
    if args and isinstance(args[0], int) and args[0] in _PERMISSION_ERRNOS:
        return True
    text = str(exc).lower()
    return "access denied" in text or "command denied" in text


class ReadOnlyConnection:
    """只读连接的轻量适配：``EXPLAIN`` 预执行 + 取数（带行数上限）。

    只经 ``ReadOnlyPool`` 取连接（红线 6：独立只读池、``read_only=True``、关多语句）。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def explain(self, sql: str) -> None:
        """对 SQL 做 ``EXPLAIN``（不返回数据）；权限类错误抛 ``ReadOnlyPermissionError``。"""
        try:
            async with self._pool.acquire() as conn:
                await conn.exec_driver_sql(f"EXPLAIN {sql}")
        except Exception as exc:  # noqa: BLE001
            if is_permission_error(exc):
                raise ReadOnlyPermissionError(str(exc)) from exc
            raise

    async def fetch(self, sql: str, *, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
        """执行只读 SQL，返回 ``(rows, truncated)``（多取一行用于判定截断）。"""
        async with self._pool.acquire() as conn:
            result = await conn.exec_driver_sql(sql)
            keys = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            rows = [dict(zip(keys, tuple(row), strict=False)) for row in fetched]
            truncated = len(rows) > max_rows
            return rows[:max_rows], truncated


class DryRunGuard:
    """只读预执行护栏（第五道）。"""

    name = "dry_run"

    def __init__(self, readonly_conn: ReadOnlyConnection) -> None:
        self._conn = readonly_conn

    async def check(self, sql: str, ctx: RunContext, scope: object | None) -> GuardResult:
        _ = (ctx, scope)
        try:
            await self._conn.explain(sql)
        except ReadOnlyPermissionError as exc:
            raise sql_guard_error(
                f"权限校验未通过：{exc}", "SQL_PERMISSION_DENIED", stage=self.name
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise sql_guard_error(str(exc), "SQL_DRY_RUN_FAILED", stage=self.name) from exc
        return GuardResult(ok=True)
