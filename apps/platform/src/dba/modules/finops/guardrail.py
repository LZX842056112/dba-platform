"""模块 10 · 预算守卫护栏（§6.5）。

对齐《设计方案 v2》§6.5、§6.1.5 与《实现要点清单》§3.26、P0-9、U10、§12.6。

设计原则（为什么护栏要「保守」）
--------------------------------
预算守卫是少数「Agent 拥有真实执行权限」的场景——它误判的代价是**业务中断**。
因此所有激进动作（降级、熔断）都要满足多重条件才触发，并且必须有全局逃生开关：

* 默认**只告警**不阻断；
* ★ **硬熔断默认关闭**（``allow_circuit_break=False``）——它是唯一会中断业务的动作；
* ★ **豁免线**（``exemption_priority``）保护关键业务（如线上故障处置 Agent）；
* **降级次数上限**（``max_downgrades_per_run``）防止一路降到底；
* **全局 kill_switch** 让运维可一键关闭所有自动动作。

★ U10（照抄 v2 §6.5 正文会怎样错）——本模块最关键的一处修法
-----------------------------------------------------------
v2 正文的 ``act()`` 写成::

    new_ctx = ctx.with_quality(decision.target_quality or "eco")
    new_ctx.extra["_downgrade_count"] = ctx.extra.get("_downgrade_count", 0) + 1

这**必然运行期报错**：``RunContext.extra`` 已是只读 ``MappingProxyType``（v2 自己在
§6.1.1 定的铁律），对只读映射赋值会 ``TypeError: 'mappingproxy' object does not support
item assignment``。即便能写，也是**改到了共享对象**上——``with_quality()`` 走的
``dataclasses.replace`` 是浅拷贝，新老上下文共享同一个 dict，兄弟分支互相污染
（§5.1 的 U10 说明）。正确写法是 ``ctx.with_downgrade(target_quality, ctx.quality)``，
降级计数与来源是**显式字段**；读取时读 ``ctx.downgrade_count``，绝不读
``ctx.extra["_downgrade_count"]``。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from dba_runtime import BudgetDecision, BudgetExceededError, RunContext

# ★ L4 是 GuardrailPolicy 的唯一 owner（capabilities.budget.guard）。
#   本模块**复用**它而不是另起一个同名类（N 系列一致性：同一语义只有一个定义）。
from dba.capabilities.budget import GuardrailPolicy

__all__ = ["BudgetGuardAgent"]

logger = logging.getLogger("dba.modules.finops.guardrail")

#: 硬限动作（``BLOCK`` 阻断；``CIRCUIT_BREAK`` 熔断）
_HardAction = Literal["BLOCK", "CIRCUIT_BREAK"]

#: 软限动作 → 是否需要「降级模型」
_SOFT_ACTIONS: frozenset[str] = frozenset(
    {"ALERT", "DOWNGRADE_MODEL", "COMPRESS_CONTEXT", "RATE_LIMIT"}
)


class BudgetGuardAgent:
    """预算守卫。Agent 名 ``budget_guard``（事件生产者表 §5.10 中
    ``budget.warning`` / ``budget.downgraded`` / ``budget.blocked`` 的唯一生产者）。
    """

    name = "budget_guard"

    def __init__(
        self,
        *,
        guard: Any,
        policy: GuardrailPolicy,
        alerts: Any = None,
        period: str = "MONTH",
    ) -> None:
        self.guard = guard
        self.policy = policy
        self.alerts = alerts
        self.period = period

    # ── Pipeline 入口（四角色流水线的 guard 步）──────────────────────
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> Any:
        """批量守卫入口：按 payload 的预估成本判定并（可选）执行动作。

        * ``estimated_micro_usd``：本轮预估成本（缺省 0，仅做余量巡检）；
        * ``apply=True`` 时对 ``DOWNGRADE_MODEL`` 走 ``act()`` 产出新上下文，
          否则只返回判定（**不产生副作用**——批量巡检默认只读）。
        """
        from dba_runtime import AgentOutput  # noqa: PLC0415 - 仅本方法需要，避免顶部循环

        estimated = int(payload.get("estimated_micro_usd", 0) or 0)
        decision = await self.evaluate(ctx, estimated)
        applied: dict[str, Any] = {}
        if payload.get("apply") and decision.action == "DOWNGRADE_MODEL":
            new_ctx = await self.act(decision, ctx)
            applied = {
                "quality": new_ctx.quality,
                "downgrade_count": new_ctx.downgrade_count,
                "downgraded_from": new_ctx.downgraded_from,
            }
        return AgentOutput(
            data={
                "budget_decision": {
                    "decision": decision.decision,
                    "action": decision.action,
                    "reason": decision.reason,
                    "target_quality": decision.target_quality,
                    "exceeded_scope": decision.exceeded_scope,
                    "applied": applied,
                }
            },
            confidence=1.0,
            meta={"agent": self.name},
        )

    # ── 判定 ────────────────────────────────────────────────────────
    async def evaluate(self, ctx: RunContext, estimated_micro_usd: int) -> BudgetDecision:
        """判定一次调用的预算状态与应采取的动作。"""
        # 1) ★ 逃生开关优先
        if self.policy.kill_switch or not self.policy.enabled:
            return BudgetDecision(decision="ALLOW", action="NONE", reason="kill_switch_on")

        # 2) ★ 豁免线：高优先级业务不参与降级
        if await self._is_exempt(ctx):
            return BudgetDecision(decision="ALLOW", action="NONE", reason="exempted")

        # 3) 预算快照 → 决策
        decision, budget = await self._check(ctx, estimated_micro_usd)
        if budget is None:
            return decision

        # 4) ★ 降级次数上限：读**显式字段** ctx.downgrade_count（U10）
        if decision.action == "DOWNGRADE_MODEL":
            if ctx.downgrade_count >= self.policy.max_downgrades_per_run:
                decision = BudgetDecision(
                    decision=decision.decision,
                    action="ALERT",
                    reason="downgrade_budget_exhausted",
                    exceeded_scope=decision.exceeded_scope,
                )

        # 5) 策略闸门：硬熔断需策略允许
        if decision.action == "CIRCUIT_BREAK" and not self.policy.allow_circuit_break:
            decision = BudgetDecision(
                decision="HARD", action="ALERT", reason="circuit_break_disabled_by_policy"
            )
        elif decision.action in _SOFT_ACTIONS and decision.action not in (
            self.policy.allowed_soft_actions()
        ):
            # 策略不允许该软限动作 → 降级为「只告警」
            decision = BudgetDecision(
                decision=decision.decision,
                action="ALERT",
                reason=f"action_disabled_by_policy:{decision.action}",
            )
        return decision

    # ── 执行 ────────────────────────────────────────────────────────
    async def act(self, decision: BudgetDecision, ctx: RunContext) -> RunContext:
        """执行动作。返回**可能被修改的** ctx（降级会改 quality 并递增降级计数）。"""
        if decision.action == "DOWNGRADE_MODEL":
            target = decision.target_quality or "eco"
            # ★ U10：必须用 with_downgrade 派生新上下文；禁止改 extra（只读）、
            #   禁止 with_quality 后手写 extra（浅拷贝 → 兄弟分支污染）。
            new_ctx = ctx.with_downgrade(target, ctx.quality)
            await self._emit(
                ctx,
                "budget.downgraded",
                {
                    "from_quality": ctx.quality,
                    "to_quality": target,
                    "downgrade_count": new_ctx.downgrade_count,
                    "reason": decision.reason,
                    "exceeded_scope": decision.exceeded_scope,
                },
            )
            return new_ctx

        if decision.action == "ALERT":
            await self._emit(
                ctx,
                "budget.warning",
                {
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "exceeded_scope": decision.exceeded_scope,
                },
            )
            if self.alerts is not None:
                try:
                    await self.alerts.raise_budget_warning(ctx, decision)
                except Exception as exc:  # noqa: BLE001 - 告警失败不得影响业务
                    logger.warning("预算告警推送失败（忽略）：%s", exc)
            return ctx

        if decision.action in ("BLOCK", "CIRCUIT_BREAK"):
            await self._emit(
                ctx,
                "budget.blocked",
                {
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "exceeded_scope": decision.exceeded_scope,
                },
            )
            # ★ 硬阻断：抛内核异常（映射到 42901），调用方据此终止本次 Run。
            raise BudgetExceededError(
                "预算硬上限/熔断阻断",
                detail={
                    "reason": decision.reason or "budget_exceeded",
                    "scope": decision.exceeded_scope,
                },
            )
        return ctx

    # ── 内部 ────────────────────────────────────────────────────────
    async def _is_exempt(self, ctx: RunContext) -> bool:
        """★ 豁免检查：该 Run 命中预算的 ``priority`` ≤ ``exemption_priority`` 则豁免。"""
        budget = await self._resolve_budget(ctx)
        if budget is None:
            return False
        return int(getattr(budget, "priority", 100)) <= self.policy.exemption_priority

    async def _check(
        self, ctx: RunContext, estimated_micro_usd: int
    ) -> tuple[BudgetDecision, Any | None]:
        """查预算快照并判定（无预算 / 无 guard → 放行）。"""
        scope = self._primary_scope(ctx)
        if self.guard is None:
            return BudgetDecision(decision="ALLOW", action="NONE", reason="no_guard"), None
        budget = await self._resolve_budget(ctx)
        if budget is None:
            return BudgetDecision(decision="ALLOW", action="NONE", reason="no_budget"), None

        snapshot: dict[str, Any] = {}
        try:
            snapshot = dict(await self.guard.snapshot(budget))
        except Exception as exc:  # noqa: BLE001 - 快照失败 → 保守放行（只告警）
            logger.warning("预算快照失败（保守放行）：%s", exc)
            return (
                BudgetDecision(
                    decision="ALLOW", action="ALERT", reason="snapshot_failed", exceeded_scope=scope
                ),
                budget,
            )

        used = int(snapshot.get("used_micro_usd", 0) or 0)
        hard = int(snapshot.get("hard_limit_micro_usd", 0) or 0)
        soft = int(snapshot.get("soft_limit_micro_usd", 0) or 0)
        breaker = str(snapshot.get("breaker_state", "CLOSED"))

        # 熔断状态优先（OPEN → 硬阻断；但 act() 会再经策略闸门确认）
        if breaker == "OPEN":
            return (
                BudgetDecision(
                    decision="HARD",
                    action="CIRCUIT_BREAK",
                    reason="breaker_open",
                    exceeded_scope=scope,
                ),
                budget,
            )
        projected = used + max(0, estimated_micro_usd)
        if hard > 0 and projected > hard:
            return (
                BudgetDecision(
                    decision="HARD",
                    action=self._hard_action(budget),
                    reason="hard_limit_exceeded",
                    exceeded_scope=scope,
                ),
                budget,
            )
        if soft > 0 and projected > soft:
            action = str(getattr(budget, "soft_action", "ALERT") or "ALERT")
            if action == "DOWNGRADE_MODEL":
                return (
                    BudgetDecision(
                        decision="SOFT",
                        action="DOWNGRADE_MODEL",
                        target_quality="eco",
                        reason="soft_limit_exceeded",
                        exceeded_scope=scope,
                    ),
                    budget,
                )
            return (
                BudgetDecision(
                    decision="SOFT",
                    action="ALERT",
                    reason="soft_limit_exceeded",
                    exceeded_scope=scope,
                ),
                budget,
            )
        return (
            BudgetDecision(
                decision="ALLOW", action="NONE", reason="within_budget", exceeded_scope=scope
            ),
            budget,
        )

    @staticmethod
    def _hard_action(budget: Any) -> _HardAction:
        """预算行上的 ``hard_action``（BLOCK / CIRCUIT_BREAK），默认 BLOCK。"""
        action = str(getattr(budget, "hard_action", "BLOCK") or "BLOCK")
        if action == "CIRCUIT_BREAK":
            return "CIRCUIT_BREAK"
        return "BLOCK"

    async def _resolve_budget(self, ctx: RunContext) -> Any | None:
        """按 ``ctx.budget_keys``（最具体 → 最宽泛）解析生效预算。"""
        scopes = self._scopes(ctx)
        if not scopes or self.guard is None:
            return None
        try:
            return await self.guard.resolve_chain(scopes, self.period)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析预算链失败（视为无预算）：%s", exc)
            return None

    @staticmethod
    def _primary_scope(ctx: RunContext) -> str | None:
        keys = ctx.budget_keys
        return keys[0] if keys else None

    @staticmethod
    def _scopes(ctx: RunContext) -> list[tuple[str, str]]:
        """把 ``budget_keys``（``"AGENT:ag_x"`` / ``"BIZ_LINE:12"`` / ``"GLOBAL:*"``）
        解析成 ``[(scope_type, scope_id)]``。"""
        scopes: list[tuple[str, str]] = []
        for key in ctx.budget_keys:
            scope_type, _, scope_id = key.partition(":")
            if scope_type:
                scopes.append((scope_type, scope_id or "*"))
        return scopes

    @staticmethod
    async def _emit(ctx: RunContext, event: str, data: dict[str, Any]) -> None:
        """经 ``ctx.emitter`` 发预算事件（唯一生产者；emitter 缺失则静默）。"""
        emitter = ctx.emitter
        if emitter is None:
            return
        try:
            await emitter.emit(event, data)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 事件发送失败不得影响预算判定
            logger.warning("预算事件发送失败（忽略）：%s", exc)
