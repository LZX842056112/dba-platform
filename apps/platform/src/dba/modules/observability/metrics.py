"""模块 03 · 三条「自研运行时价值」指标的计算（§6.4）。

对齐《设计方案 v2》§6.4 与《实现要点清单》§3.20、§5.2.5、P1-1、P1-2。

为什么这三个指标重要
--------------------
它们是「运行时是否真的在进化」的度量：

* **技能复用率** —— 被复用的技能调用 / 总技能调用。复用率不涨，说明「沉淀」是假的；
* **死技能** —— 近 30 天零调用，或调用 ≥5 次但成功率 < 20%。需要被清理或重写；
* **记忆命中率** —— 命中记忆的检索次数 / 总检索次数。反映沉淀的口径与偏好是否真被用上。

★ 复用率的统计口径（P1-2）
--------------------------
同一 Run 内多次调用同一技能**只算一次**调用——``skill_usage`` 上有
``UNIQUE(skill_id, trace_id)``，自愈重试会重复经过同一技能，v1 每 INSERT 一次就
灌水一次复用率。此处所有计数都基于 ``skill_usage`` 去重后的行数。

★ ``avg_latency_ms`` 必须按 count 加权（P1-1）：不能对「小时均值」再取平均。
本模块计算的是**日**粒度加权均值，小时 rollup 同理（见 worker ``jobs/rollup.py``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "SkillMetrics",
    "MemoryMetrics",
    "SkillMetricsCalculator",
    "MemoryHitRateCalculator",
]

logger = logging.getLogger("dba.modules.observability.metrics")


@dataclass(frozen=True, slots=True)
class SkillMetrics:
    """技能指标（写入 ``metric_daily`` 的 ``skill_total`` / ``skill_reuse_rate`` /
    ``dead_skill_count`` 三个维度）。"""

    total: int = 0
    used: int = 0
    reuse_rate: float = 0.0
    dead_count: int = 0


@dataclass(frozen=True, slots=True)
class MemoryMetrics:
    """记忆指标（``mem_lookup`` / ``mem_hit`` / ``mem_hit_rate``）。"""

    lookups: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    series: list[dict[str, Any]] = field(default_factory=list)


class SkillMetricsCalculator:
    """技能指标计算（总数 / 复用率 / 死技能）。

    数据来源是 L4 ``capabilities.skills.SkillService``（owner），本模块只读其统计，
    不直连 ``skill_registry`` / ``skill_usage`` 表（§5.9 归属矩阵）。
    """

    def __init__(self, skills: Any) -> None:
        self._skills = skills

    async def compute(self, stat_date: date, biz_line_id: int | None) -> SkillMetrics:
        """计算某日某业务线的技能指标。

        ``skills.stats(biz_line_id)`` 返回
        ``{skill_total, usage_count, reuse_count, dead_skill_count}``（owner 侧聚合）。
        """
        _ = stat_date
        stats: dict[str, Any] = {}
        if self._skills is not None:
            try:
                stats = dict(await self._skills.stats(biz_line_id))
            except Exception as exc:  # noqa: BLE001 - 统计失败降级为 0，不阻断 rollup
                logger.warning("技能统计失败（降级为空）：%s", exc)

        total = int(stats.get("skill_total", 0) or 0)
        usage_count = int(stats.get("usage_count", 0) or 0)
        reuse_count = int(stats.get("reuse_count", 0) or 0)
        dead_count = int(stats.get("dead_skill_count", 0) or 0)
        reuse_rate = (reuse_count / usage_count) if usage_count else 0.0
        return SkillMetrics(
            total=total,
            used=min(usage_count, total) if total else usage_count,
            reuse_rate=round(reuse_rate, 4),
            dead_count=dead_count,
        )


class MemoryHitRateCalculator:
    """记忆命中率计算。

    数据来源优先级：
    1) 显式传入的 ``memory``（L4 ``capabilities.memory.MemoryService``）——它有
       ``lookup_count`` / ``hit_count`` 进程内计数与 ``hit_rate()``；
    2) 否则取 ``metric_daily`` 的累计值（由 rollup 从 RunContext 的
       ``memory_lookups`` / ``memory_hits`` 汇总，见 worker ``jobs/rollup.py``）；
    3) 都没有则返回 0（诚实降级，不编造）。
    """

    def __init__(self, *, memory: Any | None = None, metric_repo: Any | None = None) -> None:
        self._memory = memory
        self._metric = metric_repo

    async def compute(self, stat_date: date, biz_line_id: int | None) -> MemoryMetrics:
        if self._memory is not None and hasattr(self._memory, "hit_rate"):
            try:
                rate = float(self._memory.hit_rate())
                lookups = int(getattr(self._memory, "lookup_count", 0) or 0)
                hits = int(getattr(self._memory, "hit_count", 0) or 0)
                return MemoryMetrics(lookups=lookups, hits=hits, hit_rate=round(rate, 4), series=[])
            except Exception as exc:  # noqa: BLE001
                logger.warning("记忆命中率读取失败（降级）：%s", exc)

        if self._metric is not None:
            try:
                rows = await self._metric.timeseries(
                    {"since": stat_date, "until": stat_date, "biz_line_id": biz_line_id}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("metric_daily 读取失败（降级）：%s", exc)
                rows = []
            lookups = sum(int(r.get("mem_lookup", 0) or 0) for r in rows)
            hits = sum(int(r.get("mem_hit", 0) or 0) for r in rows)
            rate = (hits / lookups) if lookups else 0.0
            return MemoryMetrics(lookups=lookups, hits=hits, hit_rate=round(rate, 4), series=[])

        return MemoryMetrics()
