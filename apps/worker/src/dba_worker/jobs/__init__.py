"""worker 周期任务（B5）。

对齐《设计方案 v2》§6.1.4 与《实现要点清单》§1.6.6、§5.5（路由/预算）。

八个任务（owner 关系见 §4.0 归属矩阵）：

==================  ==========================================================
任务                职责
==================  ==========================================================
``run_rollup``      ``run`` 明细 → ``metric_daily``（幂等全量重算，P2-9）
``maintain_partitions``  ``metric_daily`` 按月分区扩容（防 pmax 膨胀）
``run_anomaly_scan``  无阈值异常扫描（EWMA+MAD + 静默失败，DoD#2）
``run_daily_report``  日报快照落对象存储
``run_reconcile``     三方对账 Redis↔MySQL↔明细（DoD#4，误差 ≤ 1%）
``close_stale_runs``  滞留 Run 15 分钟收口（DoD#5）
``run_semcache_gc``   语义缓存过期清理
``run_archive``       超期 ``run`` 冷归档（默认 dry-run）
==================  ==========================================================

调用约定：每个任务都是 ``async def job(container, **kwargs) -> dict[str, Any]``，由
``scheduler.register_jobs`` 用 ``with_run_context`` 包裹后注册，保证任务内 ``ctx()`` 可用
（P1-6）。
"""

from __future__ import annotations

from .anomaly_scan import run_anomaly_scan
from .archive import run_archive
from .partition import maintain_partitions
from .reconcile import run_reconcile
from .report import run_daily_report
from .rollup import run_rollup
from .semcache_gc import run_semcache_gc
from .stale_run import close_stale_runs

__all__ = [
    "run_rollup",
    "maintain_partitions",
    "run_anomaly_scan",
    "run_daily_report",
    "run_reconcile",
    "close_stale_runs",
    "run_semcache_gc",
    "run_archive",
]
