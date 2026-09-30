"""``dba eval`` 的执行编排（§12.4 / §12.7）。

* ``--suite guard``  —— 权限/只读对抗集（离线、秒级、CI 高频）；
* ``--suite golden`` —— 分层题集 EX / 口径 / 延迟（需真实 MySQL）；
* ``--suite all``    —— 上述全部；
* ``--gate FILE``    —— 用 ``evals/thresholds.yaml`` 判定，退出码即结论。

退出码：``0`` 全部达标 / ``1`` 有套件未达标 / ``2`` 执行错误（如请求了需要 MySQL 的套件
但未提供 DSN）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .dataset import bird_mini_path, load_golden, load_jsonl
from .guard_suite import run_guard_suite
from .models import GateCheck, SuiteResult
from .report import render_markdown, to_json, write_report
from .thresholds import load_thresholds, make_check

__all__ = ["main", "build_parser"]

PROG = "dba eval"

#: 套件 → (阈值点号键 → 套件指标键)
_GATE_MAP: dict[str, dict[str, str]] = {
    "golden": {
        "chatbi.execution_accuracy.overall": "ex_overall",
        "chatbi.execution_accuracy.single_table": "ex_single_table",
        "chatbi.execution_accuracy.multi_join": "ex_multi_join",
        "chatbi.execution_accuracy.time_compare": "ex_time_compare",
        "chatbi.execution_accuracy.permission_boundary": "ex_permission_boundary",
        "chatbi.caliber_hit_rate": "caliber_hit_rate",
        "chatbi.latency.first_panel_p90_ms": "first_panel_p90_ms",
        "chatbi.latency.e2e_p90_ms": "e2e_p90_ms",
        "chatbi.cost_per_question_micro_usd_max": "cost_per_question_micro_usd",
    },
    "guard": {
        "security.scope_adversarial_pass_rate": "scope_pass_rate",
        "security.readonly_guard_pass_rate": "readonly_pass_rate",
    },
}


def _project_root() -> Path:
    """``apps/platform/src/dba/evals/runner.py`` → workspace 根（上溯 5 层）。"""
    return Path(__file__).resolve().parents[5]


def _resolve_dsn() -> str | None:
    for key in ("DBA_TEST_MYSQL_DSN", "DBA_MYSQL_DSN", "DBA_MYSQL_RO_DSN"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="运行评测套件（§12）")
    parser.add_argument(
        "--suite",
        default="all",
        choices=["all", "guard", "golden", "bird-mini"],
        help="要运行的套件",
    )
    parser.add_argument("--gate", default=None, help="阈值文件（退出码即结论：0/1/2）")
    parser.add_argument("--out", default=None, help="结果 JSON 落盘路径")
    parser.add_argument("--root", default=None, help="workspace 根（默认自动定位）")
    return parser


async def _run_suites(suite: str, root: Path, dsn: str | None) -> tuple[list[SuiteResult], bool]:
    """返回 ``(套件结果, 是否发生「请求了但无法执行」的执行错误)``。"""
    results: list[SuiteResult] = []
    exec_error = False

    if suite in ("all", "guard"):
        results.append(await run_guard_suite())

    if suite in ("all", "golden"):
        if dsn is None:
            skipped = SuiteResult(name="golden", skipped=True, skip_reason="未设置 MySQL DSN")
            results.append(skipped)
            exec_error = True
        else:
            cases = load_golden(root)
            if not cases:
                skipped = SuiteResult(
                    name="golden", skipped=True, skip_reason="golden_set.jsonl 为空"
                )
                results.append(skipped)
                exec_error = True
            else:
                from .golden_suite import run_golden_suite  # 延迟导入，无 DSN 时不触碰

                results.append(await run_golden_suite(dsn=dsn, cases=cases))

    if suite in ("all", "bird-mini"):
        rows = load_jsonl(bird_mini_path(root))
        reason = (
            "未提供 bird_mini.jsonl（BIRD 需外部下载，见 evals/README.md）"
            if not rows
            else "bird-mini 运行器未实现（本批次登记，见报告）"
        )
        results.append(SuiteResult(name="bird-mini", skipped=True, skip_reason=reason))

    return results, exec_error


def _gate_checks(suites: list[SuiteResult], thresholds_path: Path | None) -> list[GateCheck]:
    if thresholds_path is None:
        return []
    thresholds = load_thresholds(thresholds_path)
    checks: list[GateCheck] = []
    for suite in suites:
        if suite.skipped:
            continue
        for dotted, metric_key in _GATE_MAP.get(suite.name, {}).items():
            if dotted not in thresholds:
                continue
            actual = suite.metrics.get(metric_key)
            if actual is None:
                continue
            checks.append(make_check(dotted, actual, thresholds[dotted]))
    return checks


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else _project_root()
    dsn = _resolve_dsn()

    import asyncio

    try:
        suites, exec_error = asyncio.run(_run_suites(args.suite, root, dsn))
    except Exception as exc:  # noqa: BLE001 - 执行期错误统一映射为退出码 2
        print(f"[eval] 执行错误：{exc}", file=sys.stderr)
        return 2

    thresholds_path = Path(args.gate).resolve() if args.gate else None
    if thresholds_path is not None and not thresholds_path.exists():
        print(f"[eval] 门槛文件不存在：{thresholds_path}", file=sys.stderr)
        return 2

    checks = _gate_checks(suites, thresholds_path)

    if exec_error:
        verdict = 2
    elif any(not c.ok for c in checks):
        verdict = 1
    else:
        verdict = 0

    markdown = render_markdown(suites, checks, dsn_present=dsn is not None)
    print(markdown)

    if args.out:
        payload = to_json(suites, checks, verdict=verdict, dsn_present=dsn is not None)
        write_report(Path(args.out), payload)
        print(f"[eval] 结果已写入 {args.out}")

    print(f"[eval] 退出码 {verdict}（0=达标 / 1=未达标 / 2=执行错误）")
    return verdict
