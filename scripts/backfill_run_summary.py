"""一次性运维脚本：回填历史 ``run`` 行的汇总字段（R1 的收尾）。

背景
----
R1 修复了 ``di.py::_run`` 的 finish 分支——收尾时用 ``llm_call.aggregate_by_trace``
把 tokens / cost / llm_calls 聚合回填进 ``run`` 行。但**历史** run 行是修复前写入的，
汇总字段恒为 0（llm_call 明细里其实有数据）。本脚本把历史 run 行一次性回填，
使 ``metric_daily`` 的 rollup 能算出真实成本/调用数。

用法：``uv run python scripts/backfill_run_summary.py [--dry-run]``
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import sqlalchemy as sa
from dba.config import get_settings
from dba.storage.mysql import models as m
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.repo import LlmCallRepo, RunRepo

_SUMMARY_COLS = ("tokens_in", "tokens_out", "cached_tokens", "cost_micro_usd", "llm_calls")


async def _backfill(*, dry_run: bool) -> dict[str, Any]:
    settings = get_settings()
    engine = build_engine(settings.mysql_dsn)
    run_repo = RunRepo(engine)
    llm_repo = LlmCallRepo(engine)
    try:
        # 只回填「汇总字段恒 0」的行（有真实 llm_call 明细可聚合才值得回填）
        runs = await run_repo._fetch(
            sa.select(m.Run.trace_id, m.Run.started_at).where(m.Run.llm_calls == 0)
        )
        updated = 0
        skipped = 0
        for run in runs:
            trace_id = str(run["trace_id"])
            agg = await llm_repo.aggregate_by_trace(trace_id)
            if agg["llm_calls"] == 0 and agg["cost_micro_usd"] == 0:
                skipped += 1
                continue
            if dry_run:
                print(f"  [dry-run] trace={trace_id} -> {agg}")
                updated += 1
                continue
            row = {k: agg[k] for k in _SUMMARY_COLS}
            n = await run_repo.finish(trace_id, started_at=run["started_at"], row=row)
            updated += 1 if n else 0
            print(f"  [done] trace={trace_id} -> {agg}（rows={n}）")
        return {"scanned": len(runs), "updated": updated, "skipped": skipped}
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="回填历史 run 汇总字段")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落库")
    args = parser.parse_args()
    result = asyncio.run(_backfill(dry_run=args.dry_run))
    print(f"结果：{result}")
    if args.dry_run:
        print("（--dry-run：未落库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
