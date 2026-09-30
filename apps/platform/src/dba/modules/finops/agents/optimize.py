"""模块 10 · 四角色 Agent 之四：优化建议（optimize）。

对齐《设计方案 v2》§6.5 / §5.4.7 与《实现要点清单》§3.25、§7.5
``/finops/recommendations``。

职责：依据成本归因、缓存命中、死技能等信号，产出**可执行的优化建议**并落
Mongo ``finops_recommendation``（owner=finops）。

★ 建议只落库、**不自动执行**：所有 ``apply`` 必须经管理端显式确认（``dry_run`` 优先），
与护栏「保守」原则一致（§6.5）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from dba_runtime import AgentOutput, RunContext, traced

__all__ = ["OptimizeAgent"]

logger = logging.getLogger("dba.modules.finops.optimize")


class OptimizeAgent:
    """优化建议 Agent：生成建议 → 落 ``finops_recommendation``。"""

    name = "optimize"

    def __init__(self, *, reco_repo: Any = None) -> None:
        self._reco = reco_repo

    @traced("step.finops.optimize", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        recommendations = self.build(payload)
        saved = 0
        for reco in recommendations:
            if self._reco is None:
                break
            try:
                await self._reco.save(reco)
                saved += 1
            except Exception as exc:  # noqa: BLE001 - 单条落库失败不影响其余
                logger.warning("优化建议落库失败（跳过）：%s", exc)
        return AgentOutput(
            data={"recommendations": recommendations, "saved": saved},
            confidence=1.0,
            meta={"agent": self.name},
        )

    @staticmethod
    def build(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """由归因/成本摘要产出建议（纯逻辑，便于单测）。

        payload 可含：
        * ``attribution``：``AttributeAgent`` 的输出；
        * ``cost_summary``：``MeterAgent`` 的输出；
        * ``scope``：``{type, id}``。
        """
        scope = payload.get("scope") or {"type": "GLOBAL", "id": "*"}
        attribution = payload.get("attribution") or {}
        summary = payload.get("cost_summary") or {}
        out: list[dict[str, Any]] = []

        model = attribution.get("model") or {}
        if model.get("model") and int(model.get("cost_micro_usd", 0) or 0) > 0:
            cost = int(model["cost_micro_usd"])
            out.append(
                _reco(
                    scope,
                    kind="switch_model",
                    title=(
                        f"{scope.get('type')}={scope.get('id')} 的 {model['model']} "
                        "可评估换用小模型"
                    ),
                    rationale=(
                        f"该模型占本维度成本 {model.get('share')}（{cost} micro_usd）；"
                        "若其任务以结构化输出为主，换小模型通常可在成功率基本不变下显著降本"
                    ),
                    evidence={
                        "current_cost_micro_usd": cost,
                        "projected_cost_micro_usd": int(cost * 0.4),
                        "sample_size": int(summary.get("runs", 0) or 0),
                    },
                    expected_saving_micro_usd_per_month=int(cost * 0.6) * 30,
                )
            )
        return out


def _reco(
    scope: dict[str, Any],
    *,
    kind: str,
    title: str,
    rationale: str,
    evidence: dict[str, Any],
    expected_saving_micro_usd_per_month: int,
) -> dict[str, Any]:
    """构造一条 ``finops_recommendation`` 文档。"""
    return {
        "reco_id": f"rec_{uuid4().hex[:24]}",
        "scope": {"type": str(scope.get("type", "GLOBAL")), "id": scope.get("id")},
        "kind": kind,
        "title": title,
        "rationale": rationale,
        "evidence": evidence,
        "expected_saving_micro_usd_per_month": int(expected_saving_micro_usd_per_month),
        "risk": "low",
        "status": "pending",
        "created_at": datetime.now(UTC).replace(tzinfo=None),
        "applied_at": None,
        "applied_by": None,
        "rollback_plan": "回滚路由策略配置项即可（建议不自动执行）",
    }
