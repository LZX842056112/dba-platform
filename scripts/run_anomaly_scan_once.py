"""一次性脚本：手动触发一次异常扫描（R5 · 尽力项）。

背景
----
``anomaly_scan`` 是 worker 的周期任务（每 15 分钟）。这里手动触发一次，验证「无阈值
EWMA+MAD + 静默失败」检测器在真实数据上能否产出 ``alert_event``：

* 若产出告警 → 观测页/FinOps 有真实异常数据；
* 若 0 告警 → **诚实保留空态**（说明历史 run 数据量不足以触发稳健偏差），不伪造。

用法：``uv run python scripts/run_anomaly_scan_once.py [--hours 168]``
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from dba.config import get_settings
from dba.di import build_container
from dba_runtime import bind_metering
from dba_worker.jobs import close_stale_runs, run_anomaly_scan
from dba_worker.scheduler import with_run_context


async def _amain(hours: int) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    container = build_container(settings)
    bind_metering(container.get("metering"), cost=container.get("cost_normalizer"))
    try:
        print(f"== 触发异常扫描（窗口 {hours} 小时）==")
        scan = await with_run_context(run_anomaly_scan)(container, hours=hours)
        print(f"anomaly_scan -> {scan}")

        print("== 滞留 Run 收口（把 stale running 收口为 timeout）==")
        stale = await with_run_context(close_stale_runs)(container)
        print(f"close_stale_runs -> {stale}")
    finally:
        await container.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="手动触发一次异常扫描")
    parser.add_argument("--hours", type=int, default=168, help="检测时间窗（小时），默认 7 天")
    args = parser.parse_args()
    return asyncio.run(_amain(args.hours))


if __name__ == "__main__":
    raise SystemExit(main())
