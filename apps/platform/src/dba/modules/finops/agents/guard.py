"""模块 10 · ``agents/guard.py``——``BudgetGuardAgent`` 的再导出入口。

对齐《实现要点清单》§3.25（本文件列为 ``BudgetGuardAgent`` 的落点）。
实现位于 ``..guardrail``（与 ``GuardrailPolicy`` 同源，便于 U10 的单测一网打尽）。
"""

from __future__ import annotations

from ..guardrail import BudgetGuardAgent

__all__ = ["BudgetGuardAgent"]
