"""评测结果呈现（§12.7）：一张可贴进文档的结果表 + 可机读 JSON。

退出码语义（§12.4 / U23）：``0`` 全部达标 / ``1`` 有套件未达标 / ``2`` 执行错误。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import GateCheck, SuiteResult

__all__ = ["render_markdown", "to_json", "write_report"]


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def render_markdown(
    suites: list[SuiteResult],
    checks: list[GateCheck],
    *,
    dsn_present: bool,
) -> str:
    """生成结果表 + 门槛表（供报告直接引用）。"""
    lines: list[str] = ["## 评测结果（`dba eval`）", ""]
    lines.append(f"- 真实 MySQL：{'已连接' if dsn_present else '未设置（相关套件 skip）'}")
    lines.append("")
    lines.append("| 套件 | 样本 | 通过 | 主指标 | 备注 |")
    lines.append("|---|---|---|---|---|")
    for suite in suites:
        if suite.skipped:
            lines.append(f"| {suite.name} | - | - | - | ⏭ skip：{suite.skip_reason} |")
            continue
        main = suite.metrics.get("ex_overall", suite.metrics.get("pass_rate", 0.0))
        lines.append(
            f"| {suite.name} | {suite.total} | {suite.passed} | {_fmt(main)} "
            f"| 通过率 {_fmt(suite.pass_rate)} |"
        )
    lines.append("")

    for suite in suites:
        if suite.skipped:
            continue
        lines.append(f"### `{suite.name}` 分项指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        for key, value in sorted(suite.metrics.items()):
            lines.append(f"| {key} | {_fmt(value)} |")
        lines.append("")
        failures = suite.failures()
        if failures:
            lines.append("**未通过用例（前 20）**：")
            lines.append("")
            for outcome in failures[:20]:
                lines.append(f"- `{outcome.case_id}`：{outcome.detail}")
            lines.append("")

    if checks:
        lines.append("### CI 门槛比对（`evals/thresholds.yaml`）")
        lines.append("")
        lines.append("| 门槛 | 实际 | 阈值 | 结果 |")
        lines.append("|---|---|---|---|")
        for check in checks:
            arrow = "≤" if check.upper_bound else "≥"
            lines.append(
                f"| {check.name} | {_fmt(check.actual)} | {arrow} {_fmt(check.threshold)} "
                f"| {'✅' if check.ok else '❌'} |"
            )
        lines.append("")
    return "\n".join(lines)


def to_json(
    suites: list[SuiteResult], checks: list[GateCheck], *, verdict: int, dsn_present: bool
) -> dict[str, Any]:
    """机读结果（供 CI 归档）。"""
    return {
        "verdict": verdict,
        "verdict_meaning": {0: "ok", 1: "gate_failed", 2: "exec_error"}.get(verdict, "unknown"),
        "dsn_present": dsn_present,
        "suites": [
            {
                "name": s.name,
                "skipped": s.skipped,
                "skip_reason": s.skip_reason,
                "total": s.total,
                "passed": s.passed,
                "pass_rate": round(s.pass_rate, 6),
                "metrics": {k: round(v, 6) for k, v in s.metrics.items()},
                "failures": [{"id": o.case_id, "detail": o.detail} for o in s.failures()],
            }
            for s in suites
        ],
        "gate": [
            {
                "name": c.name,
                "actual": c.actual,
                "threshold": c.threshold,
                "upper_bound": c.upper_bound,
                "ok": c.ok,
            }
            for c in checks
        ],
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    """把 JSON 结果落盘（父目录自动创建）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
