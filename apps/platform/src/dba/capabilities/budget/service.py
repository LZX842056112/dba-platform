"""预算服务（★ P0-4 / P0-5 / P1-5 / P1-9 / U15 的核心实现）。

对齐《设计文档 v2》§6.5、§5.2.4 与《实现要点清单》P0-4 / P0-5 / P1-5 / P1-9 / U15。

核心不变量
----------
1. **权威账本在 MySQL**（``budget_reservation`` + ``budget_usage``），Redis 只是**快路径**。
   v1 把预留只放 Redis：Redis 一重启/驱逐，预留凭空消失，结算时账不平、无法幂等。
2. **预留一定落 MySQL**：``reserve`` 成功后 ``insert_if_absent``（同 ID 二次插入返回 False）。
3. **结算/释放幂等**：以 ``budget_reservation`` 的状态机为准——只有从 ``RESERVED`` 迁移成功
   的那次才去改 ``budget_usage``；重复调用返回 ``already_settled``/
   ``already_released`` 且**不改数**。
4. **P1-9 fail-open**：Redis 不可用时，按策略改走 MySQL **行锁原子**路径（``reserve_atomic``），
   绝不因为「Redis 挂了」就放行超限请求。
5. **P0-5**：``hard_limit_pct`` 默认 90（DB 层默认已改）；HARD 档必须能命中。
6. **P1-5**：``usage_source='self_reported'`` 的用量**不计入预算**（只释放预留，不消费）。
7. **U15**：取预算是 ``enabled=1 AND version=max`` 的那一行（改预算 = 新版本）。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dba_runtime.errors import StorageUnavailableError

from dba.capabilities.cost.price_cache import PriceCache
from dba.util.time import utcnow_naive

from .ulid import new_reservation_id

__all__ = [
    "BudgetService",
    "Decision",
    "EffectiveBudget",
    "ReservationDecision",
    "should_count_in_budget",
]

logger = logging.getLogger("dba.capabilities.budget")

Decision = Literal["allow", "soft", "hard"]


def should_count_in_budget(usage_source: str) -> bool:
    """★ P1-5：``self_reported``（供应商自报而非网关实测）的用量**不计入预算**。

    为什么：自报 token 可能被供应商口径放大/重复，若计入会误触硬熔断、误伤业务。
    实测口径（``measured``）才进预算。
    """
    return usage_source != "self_reported"


@dataclass(frozen=True)
class EffectiveBudget:
    """生效预算（U15：``version`` 最大的那一行）。"""

    budget_id: int
    scope_type: str
    scope_id: str
    period: str
    period_start: dt.date
    amount_micro_usd: int
    soft_limit_pct: int
    hard_limit_pct: int
    soft_action: str
    hard_action: str
    priority: int
    version: int
    timezone: str

    @property
    def soft_limit_micro_usd(self) -> int:
        return self.amount_micro_usd * self.soft_limit_pct // 100

    @property
    def hard_limit_micro_usd(self) -> int:
        return self.amount_micro_usd * self.hard_limit_pct // 100

    @classmethod
    def from_row(cls, row: dict[str, Any], period_start: dt.date) -> EffectiveBudget:
        return cls(
            budget_id=int(row["id"]),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            period=str(row["period"]),
            period_start=period_start,
            amount_micro_usd=int(row["amount_micro_usd"]),
            soft_limit_pct=int(row.get("soft_limit_pct", 80)),
            hard_limit_pct=int(row.get("hard_limit_pct", 90)),
            soft_action=str(row.get("soft_action", "ALERT")),
            hard_action=str(row.get("hard_action", "BLOCK")),
            priority=int(row.get("priority", 100)),
            version=int(row.get("version", 1)),
            timezone=str(row.get("timezone", "Asia/Shanghai")),
        )


@dataclass
class ReservationDecision:
    """一次预留决策的结果。"""

    decision: Decision
    budget_id: int
    reservation_id: str | None
    estimated_micro_usd: int
    used_after_micro_usd: int
    soft_limit_micro_usd: int
    hard_limit_micro_usd: int
    path: str  # "redis" | "mysql_failopen" | "denied"
    soft_action: str = "ALERT"
    hard_action: str = "BLOCK"

    @property
    def allowed(self) -> bool:
        return self.decision != "hard"

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "budget_id": self.budget_id,
            "reservation_id": self.reservation_id,
            "estimated_micro_usd": self.estimated_micro_usd,
            "used_after_micro_usd": self.used_after_micro_usd,
            "soft_limit_micro_usd": self.soft_limit_micro_usd,
            "hard_limit_micro_usd": self.hard_limit_micro_usd,
            "path": self.path,
        }


class BudgetService:
    """预算预留 / 结算 / 释放（只依赖 Protocol；缓存与 MySQL 均可选注入）。"""

    #: 价格未知时的保守预估（micro_usd）——宁可高估触发软限，也不低估放行
    DEFAULT_UNKNOWN_ESTIMATE = 20_000

    def __init__(
        self,
        *,
        budget_repo: Any,
        usage_repo: Any,
        reservation_repo: Any,
        redis_cache: Any | None = None,
        price_cache: PriceCache | None = None,
        fail_open: bool = True,
        fallback_scopes: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._budgets = budget_repo
        self._usage = usage_repo
        self._reservations = reservation_repo
        self._redis = redis_cache
        self._prices = price_cache
        self._fail_open = fail_open
        self._fallback_scopes = fallback_scopes

    # ── 预算解析（U15） ──────────────────────────────────────────────
    @staticmethod
    def period_start(
        period: str, at: dt.datetime | None = None, tz: str = "Asia/Shanghai"
    ) -> dt.date:
        """把「时刻」归到周期起点（DAY=当日 / WEEK=周一 / MONTH=当月 1 号）。"""
        zone = ZoneInfo(tz)
        moment = (at or dt.datetime.now(dt.UTC)).astimezone(zone)
        today = moment.date()
        if period == "DAY":
            return today
        if period == "WEEK":
            return today - dt.timedelta(days=today.weekday())
        return today.replace(day=1)

    async def resolve(
        self,
        scope_type: str,
        scope_id: str,
        period: str,
        *,
        at: dt.datetime | None = None,
    ) -> EffectiveBudget | None:
        """解析生效预算（U15：``enabled=1`` 且 ``version`` 最大）。"""
        row = await self._budgets.effective(scope_type, scope_id, period)
        if row is None:
            return None
        tz = str(row.get("timezone", "Asia/Shanghai"))
        start = self.period_start(period, at, tz)
        return EffectiveBudget.from_row(row, start)

    async def resolve_chain(
        self,
        scopes: list[tuple[str, str]],
        period: str,
        *,
        at: dt.datetime | None = None,
    ) -> EffectiveBudget | None:
        """按 ``scopes`` 顺序（最具体 → 最宽泛）解析第一个命中的生效预算。

        典型链：``[(AGENT, uid), (BIZ_LINE, bid), (GLOBAL, "*")]``。
        """
        ordered = list(scopes) + list(self._fallback_scopes)
        seen: set[tuple[str, str]] = set()
        for scope_type, scope_id in ordered:
            if (scope_type, scope_id) in seen:
                continue
            seen.add((scope_type, scope_id))
            budget = await self.resolve(scope_type, scope_id, period, at=at)
            if budget is not None:
                return budget
        return None

    async def resolve_by_id(
        self, budget_id: int, *, at: dt.datetime | None = None
    ) -> EffectiveBudget | None:
        """按主键取生效预算（供只读端点 ``/finops/budgets/{id}/usage`` 使用）。

        ★ 直接用 id 定位（而非 ``resolve`` 的 scope 匹配），因为该端点按 id 寻址。
        """
        row = await self._budgets.get(budget_id)
        if row is None:
            return None
        tz = str(row.get("timezone", "Asia/Shanghai"))
        period = str(row.get("period", "MONTH"))
        start = self.period_start(period, at, tz)
        return EffectiveBudget.from_row(row, start)

    # ── 预估 ────────────────────────────────────────────────────────
    def estimate(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        max_completion_tokens: int,
    ) -> int:
        """预估一次调用的成本（用于预留）。价格未知 → 保守默认值。"""
        if self._prices is None:
            return self.DEFAULT_UNKNOWN_ESTIMATE
        price = self._prices.get(provider, model)
        if price is None:
            return self.DEFAULT_UNKNOWN_ESTIMATE
        unit = price.unit_size
        inp = (max(0, prompt_tokens) * price.input_price_micro_usd + unit // 2) // unit
        out = (max(0, max_completion_tokens) * price.output_price_micro_usd + unit // 2) // unit
        return inp + out

    # ── 预留（P0-4 + P1-9） ─────────────────────────────────────────
    async def reserve(
        self,
        budget: EffectiveBudget,
        *,
        estimated_micro_usd: int,
        trace_id: str | None = None,
        reservation_id: str | None = None,
    ) -> ReservationDecision:
        """原子预留。返回决策（含 ``reservation_id``，用于后续 settle/release）。"""
        rid = reservation_id or new_reservation_id()
        hard = budget.hard_limit_micro_usd
        soft = budget.soft_limit_micro_usd
        period_key = budget.period_start.isoformat()

        granted = False
        used_after = 0
        path = "denied"

        if self._redis is not None:
            try:
                res = await self._redis.reserve(
                    budget_id=budget.budget_id,
                    period_start=period_key,
                    reservation_id=rid,
                    amount=estimated_micro_usd,
                    hard_limit=hard,
                )
                # code: 1=granted, 2=idempotent(已存在，视为已预留), 0=denied
                granted = int(res["code"]) in (1, 2)
                used_after = int(res["used"])
                path = "redis"
            except Exception as exc:  # noqa: BLE001 - Redis 故障 → 走 fail-open
                logger.warning("Redis 预留失败，按策略处理：%s", exc)
                if not self._fail_open:
                    raise StorageUnavailableError(
                        "Redis 不可用且策略为 fail_closed，拒绝预留",
                        detail={"component": "redis"},
                    ) from exc
                path = "mysql_failopen"

        if path == "mysql_failopen":
            atomic = await self._usage.reserve_atomic(
                budget.budget_id, budget.period_start, amount=estimated_micro_usd, hard_limit=hard
            )
            granted = bool(atomic["granted"])
            used_after = int(atomic["used"])

        if not granted:
            return ReservationDecision(
                decision="hard",
                budget_id=budget.budget_id,
                reservation_id=None,
                estimated_micro_usd=estimated_micro_usd,
                used_after_micro_usd=used_after,
                soft_limit_micro_usd=soft,
                hard_limit_micro_usd=hard,
                path=path,
                soft_action=budget.soft_action,
                hard_action=budget.hard_action,
            )

        # ★ P0-4：预留**一定落 MySQL 权威账本**（幂等；Redis 只是快路径）
        await self._reservations.insert_if_absent(
            {
                "reservation_id": rid,
                "budget_id": budget.budget_id,
                "period_start": budget.period_start,
                "trace_id": trace_id,
                "estimated_micro_usd": estimated_micro_usd,
                "actual_micro_usd": None,
                "state": "RESERVED",
                "decision": 1,
                "expires_at": _utcnow_naive() + dt.timedelta(hours=1),
            }
        )

        decision: Decision = "soft" if used_after > soft else "allow"
        return ReservationDecision(
            decision=decision,
            budget_id=budget.budget_id,
            reservation_id=rid,
            estimated_micro_usd=estimated_micro_usd,
            used_after_micro_usd=used_after,
            soft_limit_micro_usd=soft,
            hard_limit_micro_usd=hard,
            path=path,
            soft_action=budget.soft_action,
            hard_action=budget.hard_action,
        )

    # ── 结算（P0-4 幂等） ───────────────────────────────────────────
    async def settle(
        self,
        budget: EffectiveBudget,
        *,
        reservation_id: str,
        actual_micro_usd: int,
        usage_source: str = "measured",
    ) -> dict[str, Any]:
        """结算预留。

        ★ 幂等以 ``budget_reservation`` 状态机为准：只有本次把 ``RESERVED → SETTLED``
        成功搬迁的那一次，才去改 ``budget_usage``；重复调用**不改数**。

        ★ P1-5：``usage_source='self_reported'`` → 转「释放」（不消费）。
        """
        if not should_count_in_budget(usage_source):
            released = await self.release(budget, reservation_id=reservation_id)
            released["reason"] = "self_reported_not_counted"
            return released

        row = await self._reservations.settle(reservation_id, int(actual_micro_usd))
        status = str(row["status"])
        estimated = int(row["estimated_micro_usd"])

        if status == "settled":
            await self._usage.apply_settle(
                budget.budget_id,
                budget.period_start,
                released=estimated,
                actual=int(actual_micro_usd),
            )
            await self._redis_settle(budget, reservation_id, int(actual_micro_usd))

        return {
            "status": status,
            "estimated_micro_usd": estimated,
            "actual_micro_usd": int(actual_micro_usd),
            "counted": should_count_in_budget(usage_source),
        }

    # ── 释放（P0-4 幂等） ───────────────────────────────────────────
    async def release(self, budget: EffectiveBudget, *, reservation_id: str) -> dict[str, Any]:
        """释放预留。幂等：重复释放返回 ``already_released`` 且不改数。"""
        row = await self._reservations.release(reservation_id)
        status = str(row["status"])
        estimated = int(row["estimated_micro_usd"])

        if status == "released":
            await self._usage.apply_release(budget.budget_id, budget.period_start, estimated)
            await self._redis_release(budget, reservation_id)

        return {"status": status, "estimated_micro_usd": estimated}

    # ── 快照 / 熔断 ─────────────────────────────────────────────────
    async def snapshot(self, budget: EffectiveBudget) -> dict[str, Any]:
        """读取权威用量快照（MySQL）+ 快路径用量（Redis）。"""
        usage = await self._usage.get(
            budget.budget_id, budget.period_start
        ) or await self._usage.get_or_create(budget.budget_id, budget.period_start)
        consumed = int(usage.get("consumed_micro_usd", 0) or 0)
        reserved = int(usage.get("reserved_micro_usd", 0) or 0)
        return {
            "budget_id": budget.budget_id,
            "period_start": budget.period_start.isoformat(),
            "consumed_micro_usd": consumed,
            "reserved_micro_usd": reserved,
            "used_micro_usd": consumed + reserved,
            "soft_limit_micro_usd": budget.soft_limit_micro_usd,
            "hard_limit_micro_usd": budget.hard_limit_micro_usd,
            "used_pct": (consumed + reserved) * 100 // budget.amount_micro_usd
            if budget.amount_micro_usd
            else 0,
            "breaker_state": str(usage.get("breaker_state", "CLOSED")),
        }

    async def set_breaker(self, budget: EffectiveBudget, state: str) -> None:
        await self._usage.set_breaker(budget.budget_id, budget.period_start, state)

    async def expire_stale_reservations(self) -> list[dict[str, Any]]:
        """过期未结算的预留 → ``EXPIRED`` 并退回额度（worker 定时调用）。"""
        rows = await self._reservations.expire_stale(_utcnow_naive())
        for row in rows:
            await self._usage.apply_release(
                int(row["budget_id"]), row["period_start"], int(row["estimated_micro_usd"])
            )
        return list(rows)

    # ── Redis 快路径 best-effort ────────────────────────────────────
    async def _redis_settle(
        self, budget: EffectiveBudget, reservation_id: str, actual: int
    ) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.settle(
                budget_id=budget.budget_id,
                period_start=budget.period_start.isoformat(),
                reservation_id=reservation_id,
                actual=actual,
            )
        except Exception:  # noqa: BLE001 - 快路径失败不影响权威账本
            logger.warning("Redis 结算失败（权威账本已更新）", exc_info=True)

    async def _redis_release(self, budget: EffectiveBudget, reservation_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.release(
                budget_id=budget.budget_id,
                period_start=budget.period_start.isoformat(),
                reservation_id=reservation_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Redis 释放失败（权威账本已更新）", exc_info=True)


def _utcnow_naive() -> dt.datetime:
    """MySQL ``DATETIME(3)`` 存 UTC 无时区；统一用 naive UTC（§6.2）。"""
    return utcnow_naive()
