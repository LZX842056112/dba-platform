"""预算守卫（内核 L2）。

对齐《设计方案 v2》§6.1.5 / 《实现要点清单》§5.5、§5.8(U8)。

实现放在 L4 ``capabilities/budget``（Redis Lua 原子预留 + MySQL ``budget_reservation`` /
``budget_usage`` 账本）。内核只声明 Protocol 与数据模型。
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from .context import RunContext

__all__ = [
    "BudgetDecision",
    "Reservation",
    "SettlementResult",
    "BudgetUsage",
    "BudgetGuard",
]


class BudgetDecision(BaseModel):
    """预算判定结果。"""

    decision: Literal["ALLOW", "SOFT", "HARD", "BLOCK"]
    action: Literal[
        "NONE",
        "ALERT",
        "DOWNGRADE_MODEL",
        "COMPRESS_CONTEXT",
        "RATE_LIMIT",
        "BLOCK",
        "CIRCUIT_BREAK",
    ] = "NONE"
    target_quality: Literal["eco", "std", "max"] | None = None
    exceeded_scope: str | None = None
    reason: str | None = None


class Reservation(BaseModel):
    """★ v2 新增：预留凭据。

    v1 的 ``reserve()`` 只返回一个字符串 id，但这个 id 在 Redis / MySQL 里都没有落点，
    ``release`` / ``settle`` 无从知道金额。
    """

    reservation_id: str  # ULID，调用方生成（幂等键）
    budget_id: int
    period_start: date
    estimated_micro_usd: int
    decision: Literal["ALLOW", "SOFT", "HARD"]
    granted: bool


class SettlementResult(BaseModel):
    """★ v2 新增：结算结果。``settle`` 幂等，重复调用返回 ``already_settled``。"""

    status: Literal["settled", "already_settled", "reservation_missing"]
    consumed_micro_usd: int
    reserved_micro_usd: int


class BudgetUsage(BaseModel):
    """★ U8：文档引用未定义，此处补最小定义（``usage()`` 只读查询的返回）。"""

    budget_id: int
    scope_type: str
    scope_id: str
    period_start: date
    amount_micro_usd: int
    consumed_micro_usd: int = 0
    reserved_micro_usd: int = 0
    breaker_state: Literal["CLOSED", "HALF_OPEN", "OPEN"] = "CLOSED"

    @property
    def remaining_micro_usd(self) -> int:
        """剩余可用额度 = 额度 − 已消耗 − 已预留（可为负，表示已超支）。"""
        return self.amount_micro_usd - self.consumed_micro_usd - self.reserved_micro_usd


@runtime_checkable
class BudgetGuard(Protocol):
    """预算守卫。``reserve`` / ``settle`` / ``release`` **全部幂等**。"""

    async def check(self, ctx: RunContext, estimated_micro_usd: int) -> BudgetDecision: ...

    async def reserve(
        self, ctx: RunContext, estimated_micro_usd: int, reservation_id: str
    ) -> Reservation:
        """★ v2：Redis Lua 原子 check-and-reserve。

        - ``reservation_id`` 由调用方生成并贯穿 ``settle`` / ``release``；
        - 同一 id 重复预留**幂等**（返回首次结果，不重复占用）；
        - 预留明细经 outbox 落 MySQL ``budget_reservation``（权威账本）；
        - 超硬上限时 ``granted=False``，调用方抛 ``BudgetExceededError``。
        """
        ...

    async def settle(self, reservation_id: str, actual_micro_usd: int) -> SettlementResult:
        """★ v2：幂等结算。

        重复 settle 返回 ``already_settled``，不再累加 consumed；预留不存在
        （已过期 / Redis 被清空）时以 MySQL ``budget_reservation`` 为准补账。
        """
        ...

    async def release(self, reservation_id: str, reason: str) -> None:
        """★ v2：幂等释放。

        失败 / 超时 / 用户取消都要归还预留；未归还的预留由 worker 按 ``expires_at``
        回收并计入告警。
        """
        ...

    async def usage(self, scope: str, period_start: date) -> BudgetUsage:
        """只读查询。

        ★ 这是 §5.9 归属矩阵里对外暴露的读取接口——03/10 的成本大屏只能走它，
        不能直连 ``budget_usage`` 表。
        """
        ...
