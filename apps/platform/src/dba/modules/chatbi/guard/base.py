"""SQL 护栏链（L3 · ChatBI · 安全红线）。

对齐《设计方案 v2》§6.3.1 / §6.3.4 与《实现要点清单》§5.8、N7。

★ 关键约束（照抄 v2 正文会踩的坑）
-----------------------------------
1) **异常必须复用内核的 ``SqlGuardError``**（N7）。v2 §6.3.1 正文里又定义了一个同名类
   ``class SqlGuardError(Exception)``；若照抄，``Pipeline._classify`` 就认不出它，
   于是「越权短路不重试」失效——护栏拒绝会被当成普通异常被反复回退重试（刷 token）。
   本模块通过 ``from dba_runtime.errors import SqlGuardError`` **直接复用**内核类，
   只额外提供一个 ``sql_guard_error(...)`` 构造助手把 **符号码** 写进 ``symbol`` 字段。
2) ``SqlGuard`` 实现必须**无副作用**：返回改写后的 SQL（如需），不改上下文。
3) 护栏链**任一失败即抛错**，并先写 ``sql_audit``（deny）；全部通过后写一条 rewrite 审计。
   审计是「事后可追责」的唯一凭证，不能在失败路径上省略。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dba_runtime.context import RunContext
from dba_runtime.errors import SqlGuardError
from pydantic import BaseModel

from dba.storage.protocols import SqlAuditRepo

if TYPE_CHECKING:  # 仅类型标注，避免与 row_scope 形成运行期循环
    from .row_scope import ScopePredicate

__all__ = [
    "GuardResult",
    "SqlGuardError",
    "SqlGuard",
    "SqlGuardChain",
    "sql_guard_error",
]


class GuardResult(BaseModel):
    """单条护栏的判定结果。"""

    ok: bool
    rewritten_sql: str | None = None
    scope_injected: bool = False
    reason: str | None = None
    code: str | None = None


def sql_guard_error(message: str, symbol: str, *, stage: str = "unknown") -> SqlGuardError:
    """构造内核 ``SqlGuardError``（★ N7：复用内核类，只补符号码与阶段）。

    ``symbol`` 取值见《实现要点清单》§5.8 错误码表，例如 ``SQL_MULTI_STATEMENT`` /
    ``SQL_NOT_READONLY`` / ``SQL_SCOPE_EMPTY`` / ``SQL_SCOPE_NOT_INJECTED`` /
    ``SQL_PERMISSION_DENIED`` / ``SQL_DRY_RUN_FAILED``。
    ``stage`` 记录是哪一道护栏拒绝的（写入 ``sql_audit.guard_stage``）。
    """
    err = SqlGuardError(message, symbol=symbol)
    # 内核 DbaError 无 __slots__，可挂附加属性；仅用于审计与排障，不影响 _classify。
    err.stage = stage  # type: ignore[attr-defined]
    return err


@runtime_checkable
class SqlGuard(Protocol):
    """单条护栏。实现必须无副作用，返回改写后的 SQL（如需）。"""

    name: str

    async def check(
        self, sql: str, ctx: RunContext, scope: ScopePredicate | None
    ) -> GuardResult: ...


class SqlGuardChain:
    """按序执行护栏。任一失败即抛 ``SqlGuardError``，触发 Pipeline 回退。"""

    def __init__(self, guards: list[SqlGuard], audit: SqlAuditRepo) -> None:
        self.guards = guards
        self.audit = audit

    async def validate(
        self, sql: str, ctx: RunContext, scope: ScopePredicate | None
    ) -> GuardResult:
        """顺序执行护栏，返回聚合结果（含改写 SQL / 是否注入权限）。

        * 任一护栏抛 ``SqlGuardError``：写 deny 审计后原样抛出；
        * 任一护栏返回 ``ok=False``：写 deny 审计后抛 ``SqlGuardError``；
        * 全部通过：写 rewrite 审计，返回最终 SQL。
        """
        original, current = sql, sql
        scope_injected = False
        for guard in self.guards:
            try:
                res = await guard.check(current, ctx, scope)
            except SqlGuardError as exc:
                await self._write_deny(original, ctx, guard.name, str(exc), exc.symbol)
                raise
            if not res.ok:
                await self._write_deny(
                    original, ctx, guard.name, res.reason or "校验未通过", res.code
                )
                raise sql_guard_error(
                    res.reason or "校验未通过",
                    res.code or "SQL_GUARD_BLOCKED",
                    stage=guard.name,
                )
            if res.rewritten_sql:
                current = res.rewritten_sql
            scope_injected = scope_injected or res.scope_injected

        await self.audit.write(
            trace_id=ctx.trace_id,
            sql_text=original,
            rewritten_sql=current,
            decision="rewrite",
            scope_injected=scope_injected,
            user_id=ctx.user_id,
            biz_line_id=ctx.biz_line_id,
        )
        return GuardResult(ok=True, rewritten_sql=current, scope_injected=scope_injected)

    async def _write_deny(
        self,
        sql: str,
        ctx: RunContext,
        stage: str,
        reason: str,
        code: str | None,
    ) -> None:
        await self.audit.write(
            trace_id=ctx.trace_id,
            sql_text=sql,
            decision="deny",
            guard_stage=stage,
            deny_reason=reason,
            code=code,
            user_id=ctx.user_id,
            biz_line_id=ctx.biz_line_id,
        )
