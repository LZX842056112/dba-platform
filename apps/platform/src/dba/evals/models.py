"""评测数据模型（纯数据类，无 IO 副作用）。

对齐《设计方案 v2》§12.2 / §12.3 / §12.7。

设计取舍
--------
* 所有模型都是**不可变/轻量**数据类，便于在 `run_eval` 与报告之间传递；
* 指标一律用 ``dict[str, float]`` 承载（布尔也折算为 0.0/1.0），
  以便统一走门槛比较逻辑（见 ``thresholds.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "GoldenCase",
    "GuardCase",
    "CaseOutcome",
    "SuiteResult",
    "GateCheck",
]


@dataclass(frozen=True)
class GoldenCase:
    """``evals/golden_set.jsonl`` 的一行（§12.2 记录格式）。

    ``candidate_sql`` 是**录播的模型输出**：在无外部 LLM 的环境下，用它充当
    「模型生成的 SQL」，从而让 EX 的测量保持确定性、可复现（详见 ``evals/README.md``）。
    ``candidate_metric_codes`` 同理，用于口径命中率。
    """

    id: str
    question: str
    category: str
    role: str
    metric_codes: tuple[str, ...] = ()
    gold_sql: str = ""
    candidate_sql: str = ""
    candidate_metric_codes: tuple[str, ...] = ()
    expect: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def expects_denied(self) -> bool:
        """该题期望被护栏拒绝（权限边界题）。"""
        return bool(self.expect.get("denied"))

    @property
    def expected_code(self) -> str | None:
        code = self.expect.get("code")
        return str(code) if code else None


@dataclass(frozen=True)
class GuardCase:
    """一条对抗用例（§12.5）。

    ``group`` 取值：``readonly``（只读/危险构造）/ ``scope``（行级注入与拒绝）/
    ``assertion``（兜底断言直测）/ ``cache``（scope_hash 串权）。
    ``expected`` 为 ``deny symbol``（期望抛错）或 ``"inject"``（期望改写并注入）。
    """

    id: str
    group: str
    sql: str
    role: str
    expected: str
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class CaseOutcome:
    """单条用例的执行结果。"""

    case_id: str
    passed: bool
    detail: str = ""
    latency_ms: int = 0


@dataclass
class SuiteResult:
    """一个评测套件的汇总。"""

    name: str
    outcomes: list[CaseOutcome] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str = ""

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def failures(self) -> list[CaseOutcome]:
        """返回全部未通过用例（用于报告定位）。"""
        return [o for o in self.outcomes if not o.passed]


@dataclass(frozen=True)
class GateCheck:
    """一条门槛比对结果。"""

    name: str
    actual: float
    threshold: float
    upper_bound: bool
    ok: bool

    def describe(self) -> str:
        arrow = "≤" if self.upper_bound else "≥"
        mark = "✅" if self.ok else "❌"
        return f"{mark} {self.name}: {self.actual:g} {arrow} {self.threshold:g}"
