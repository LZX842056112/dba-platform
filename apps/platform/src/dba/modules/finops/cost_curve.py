"""模块 10 · 复用率-成本曲线（§6.5 ``CostCurveAnalyzer``）。

对齐《设计方案 v2》§6.5 / §7.5 与《实现要点清单》§3.28、B5-DoD③、§13 追问 9。

这是本模块最有说服力的一张图的数据：横轴是**技能复用率**，纵轴是**同类任务的平均
token 消耗**。证明「技能复用率 ↑ → 重复任务 token ↓」。

★ 必须带对照组（v2 反复强调，§7.5 的 interpretation 提醒）
----------------------------------------------------------
只画「复用率上升 + 成本下降」会被质疑是「业务本身变简单了」。因此返回值同时包含：

* ``group="hit"``：**命中**技能的同类任务平均 token；
* ``group="miss"``：**未命中**技能的同类任务平均 token（**对照组**）。

命中组下降、对照组基本持平 → 才能说明下降来自技能复用而不是其它因素。并给出
两者与复用率的**皮尔逊相关系数**与样本量。

★ 归属说明（§5.9 与「已登记文档缺口」）
-------------------------------------
``skill_usage`` 的 owner 是 ``capabilities.skills``、``llm_call`` 的 owner 是
``capabilities.telemetry``——二者都**不属于** finops。因此本分析器不直连任何表，
而是经注入的 ``CurveSource``（由 DI 用允许的 owner 读接口构造）取每日点位。
「L4 未暴露『按天 × 命中/未命中 的平均 token』读接口」登记为报告「遗留问题」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DailyPoint",
    "CurveData",
    "CurveSource",
    "CostCurveAnalyzer",
    "pearson_correlation",
]

logger = logging.getLogger("dba.modules.finops.cost_curve")


@dataclass(slots=True)
class DailyPoint:
    """单日点位。

    * ``reuse_rate``：当日技能复用率（0~1）；
    * ``tokens_hit`` / ``tokens_miss``：命中 / 未命中技能的同类任务**平均** token；
    * ``hit_samples`` / ``miss_samples``：两组样本量（相关系数与对照的可信度依赖它）；
    * ``saved_micro_usd``：当日技能复用估算节省（可选，来自 ``skill_usage`` / rollup）。
    """

    day: date
    reuse_rate: float = 0.0
    tokens_hit: float = 0.0
    tokens_miss: float = 0.0
    hit_samples: int = 0
    miss_samples: int = 0
    saved_micro_usd: int = 0


@dataclass(slots=True)
class CurveData:
    """``GET /finops/curve/reuse-vs-token`` 的返回。"""

    biz_line_id: int
    days: int
    reuse_series: list[dict[str, Any]] = field(default_factory=list)
    token_series: list[dict[str, Any]] = field(default_factory=list)
    correlation: float | None = None
    total_saved_micro_usd: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "biz_line_id": self.biz_line_id,
            "days": self.days,
            "reuse_series": self.reuse_series,
            "token_series": self.token_series,
            "correlation": self.correlation,
            "total_saved_micro_usd": self.total_saved_micro_usd,
            "note": self.note,
        }


@runtime_checkable
class CurveSource(Protocol):
    """曲线数据源：返回按日升序的点位。"""

    async def daily(self, biz_line_id: int, days: int) -> list[DailyPoint]: ...


def pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数；样本不足或方差为 0 时返回 ``None``（不编造）。"""
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return float(round(num / (dx * dy), 4))


class CostCurveAnalyzer:
    """★ 复用率-成本曲线分析器（带对照组 + 相关系数 + 累计节省）。"""

    def __init__(
        self, source: CurveSource | None = None, *, price_micro_per_token: int = 0
    ) -> None:
        self._source = source
        #: 可选：把「节省的 token」折算成微美元（未配置则只报 token 口径，不编造金额）
        self._price_per_token = price_micro_per_token

    async def reuse_vs_token(self, biz_line_id: int, days: int = 30) -> CurveData:
        """取「技能复用率」与「命中/未命中 同类任务平均 token」两条线 + 相关系数。"""
        points: list[DailyPoint] = []
        if self._source is not None:
            try:
                points = list(await self._source.daily(biz_line_id, days))
            except Exception as exc:  # noqa: BLE001 - 数据源失败 → 返回空曲线（不编造）
                logger.warning("曲线数据源失败（返回空曲线）：%s", exc)
        return self.analyze(biz_line_id, days, points)

    def analyze(self, biz_line_id: int, days: int, points: list[DailyPoint]) -> CurveData:
        """由点位计算曲线（纯逻辑，便于单测）。"""
        ordered = sorted(points, key=lambda p: p.day)
        reuse_series = [
            {"date": p.day.isoformat(), "reuse_rate": round(p.reuse_rate, 4)} for p in ordered
        ]

        token_series: list[dict[str, Any]] = []
        for point in ordered:
            if point.hit_samples:
                token_series.append(
                    {
                        "date": point.day.isoformat(),
                        "avg_tokens_per_task": round(point.tokens_hit, 2),
                        "group": "hit",
                        "samples": point.hit_samples,
                    }
                )
            if point.miss_samples:
                # ★ 对照组：未命中技能的同任务 token 序列
                token_series.append(
                    {
                        "date": point.day.isoformat(),
                        "avg_tokens_per_task": round(point.tokens_miss, 2),
                        "group": "miss",
                        "samples": point.miss_samples,
                    }
                )

        # ★ 相关系数：复用率(x) vs 命中组平均 token(y)。预期为负（复用率↑→token↓）。
        xs = [p.reuse_rate for p in ordered if p.hit_samples]
        ys = [p.tokens_hit for p in ordered if p.hit_samples]
        correlation = pearson_correlation(xs, ys)

        total_saved = self._saved_micro_usd(ordered)
        return CurveData(
            biz_line_id=biz_line_id,
            days=days,
            reuse_series=reuse_series,
            token_series=token_series,
            correlation=correlation,
            total_saved_micro_usd=total_saved,
            note=_build_note(ordered, correlation),
        )

    def _saved_micro_usd(self, points: list[DailyPoint]) -> int:
        """累计节省（微美元）。

        优先用点位自带 ``saved_micro_usd``（来自 ``skill_usage`` 的权威节省）；
        否则若配置了 ``price_micro_per_token``，用「(对照组 − 命中组) × 命中样本数」
        估算节省的 token 再折算。**未配置价格时返回 0**——不编造金额。
        """
        saved = sum(int(p.saved_micro_usd) for p in points)
        if saved:
            return max(0, saved)
        if not self._price_per_token:
            return 0
        saved_tokens = sum(max(0.0, p.tokens_miss - p.tokens_hit) * p.hit_samples for p in points)
        return max(0, int(saved_tokens * self._price_per_token))


def _build_note(points: list[DailyPoint], correlation: float | None) -> str:
    """生成可读结论（含对照组对比，避免伪相关）。"""
    if not points:
        return "无数据：需要按天的技能复用率与命中/未命中 token 明细"
    hit = [p.tokens_hit for p in points if p.hit_samples]
    miss = [p.tokens_miss for p in points if p.miss_samples]
    segments: list[str] = []
    if hit and miss:
        if hit[0]:
            drop = (hit[0] - hit[-1]) / hit[0] * 100
            segments.append(f"命中技能的同类任务平均 token 变化 {drop:+.1f}%")
        if miss[0]:
            miss_drop = (miss[0] - miss[-1]) / miss[0] * 100
            segments.append(f"未命中对照组变化 {miss_drop:+.1f}%（应基本持平）")
    if correlation is not None:
        segments.append(f"复用率与命中组 token 相关系数 {correlation:+.2f}")
    return "；".join(segments) or "样本不足，无法给出结论"
