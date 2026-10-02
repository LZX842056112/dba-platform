"""④ 行数与超时护栏（LIMIT 归一 + MySQL 优化器超时提示）。

对齐《设计方案 v2》§6.3.2 ④ 与《实现要点清单》§5.8。

★ v2 的修法（照抄 v1 会怎样错）
------------------------------
v1 只改了 ``LIMIT``——声明里的 ``timeout_s`` **从未生效**，一条慢查询仍能长时间占住
连接。v2 在 SQL 层挂 MySQL 优化器提示 ``/*+ MAX_EXECUTION_TIME(ms) */``，
应用侧再由 ``QueryExecutor`` 叠加 ``asyncio.timeout``，形成**双保险**。
"""

from __future__ import annotations

from typing import Any

from dba_runtime.context import RunContext
from sqlglot import exp

from .ast_utils import parse_one_or_deny
from .base import GuardResult

__all__ = ["LimitGuard", "apply_limit", "apply_max_execution_time"]


class LimitGuard:
    """行数上限 + 语句级超时提示。"""

    name = "limit"

    def __init__(self, max_rows: int = 5000, timeout_s: float = 30.0) -> None:
        self.max_rows = max_rows
        self.timeout_s = timeout_s

    async def check(self, sql: str, ctx: RunContext, scope: object | None) -> GuardResult:
        """④ 道：补 / 压 ``LIMIT``，再挂 ``MAX_EXECUTION_TIME`` 优化器提示。

        输入：待改写 SQL、上下文、权限子域（本护栏不消费）。
        输出：``GuardResult(ok=True, rewritten_sql=...)``——**总是返回改写后的 SQL**。
        注意：提示挂载失败不抛错（会话层 + 应用层 timeout 仍生效，见模块 docstring）。
        """
        _ = (ctx, scope)
        tree = parse_one_or_deny(sql, stage=self.name)
        tree = apply_limit(tree, self.max_rows)
        tree = apply_max_execution_time(tree, int(self.timeout_s * 1000))
        return GuardResult(ok=True, rewritten_sql=tree.sql(dialect="mysql"))


def apply_limit(tree: Any, max_rows: int) -> Any:
    """无 ``LIMIT`` → 补；超上限 → 压到上限；非字面量 ``LIMIT`` → 直接压到上限。"""
    if max_rows <= 0:
        return tree
    current = _existing_limit(tree)
    if current is not None and current <= max_rows:
        return tree
    return tree.limit(max_rows)


def apply_max_execution_time(tree: Any, timeout_ms: int) -> Any:
    """挂 MySQL 优化器提示 ``MAX_EXECUTION_TIME(ms)``（失败则原样返回）。

    提示只是「双保险」的一环——``ReadOnlyPool`` 已在会话层设置 ``max_execution_time``，
    应用侧还有 ``asyncio.timeout``。因此这里失败**不致命**，不抛错。
    """
    if timeout_ms <= 0:
        return tree
    try:
        return tree.hint(f"MAX_EXECUTION_TIME({timeout_ms})", dialect="mysql")
    except Exception:  # noqa: BLE001 - 提示失败不阻断（会话层 + 应用层仍生效）
        return tree


def _existing_limit(tree: Any) -> int | None:
    limit = tree.args.get("limit")
    if limit is None:
        return None
    expression = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(expression, exp.Literal):
        try:
            return int(str(expression.this))
        except (TypeError, ValueError):
            return None
    return None
