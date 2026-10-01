"""模块 03 · 无阈值异常检测（EWMA 基线 + MAD 稳健偏差）。

对齐《设计方案 v2》§6.4（``AnomalyDetector`` / ``SilentFailureDetector``）与
《实现要点清单》§3.21、§4.0 归属矩阵、P1-4、U14。

为什么是「无阈值」而不是「给每个指标配一个阈值」
------------------------------------------------
阈值需要人工维护，且业务有季节性（大促、月末结算），配不准就会「大异常检不出、
小波动天天报」。这里改为为每个 (指标, 维度) 维护一条 **EWMA 基线**，用 **MAD**
算稳健偏差，偏离超过 k 倍 MAD 即判定异常：:

    base_t     = α * value_t + (1 - α) * base_{t-1}          # α = 0.3
    MAD        = median(|x_i - median(x)|)
    robust_z   = 0.6745 * (latest - base) / MAD              # 0.6745 = Φ⁻¹(0.75)
    判定        |robust_z| > k（默认 3.5）

为什么用 MAD 而不是标准差：标准差会被异常点本身拉大，导致「异常越大越检测不出来」；
MAD 对离群点稳健（这正是 v2 强调的取舍）。

★ 两处与 v2 正文的**有意差异**（都是为了让检测真正可用）
--------------------------------------------------------
1. **基线与 MAD 只用「历史窗口」（不含待检点）计算**。若含待检点，一个尖峰会把 EWMA
   与 MAD 一起拉高——尖峰越大，``latest - base`` 反而越被压低，检测灵敏度随异常幅度
   下降。这与「用 MAD 而非标准差」是同一个道理。
2. **``mad <= 0`` 直接跳过**（文档原文如此）：序列毫无波动时不判异常，避免把
   「常数序列 + 一个点」这种冷启动数据误报。

★ P1-4 自观测污染（照抄 v1 会怎样错）
------------------------------------
v1 把平台自身的 Run 也计入指标：03 的扫描任务、10 的对账任务跑得越久、调用越多，
就越是把自己算成异常，甚至给「自己」报警。因此 ``_load_series`` 必须按
``exclude_modules=("observability", "finops")`` 排除平台自身模块；平台自身消耗改由
独立视图 ``GET /obs/self-cost`` 展示（见 §7.6）。
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

__all__ = [
    "SELF_MODULES",
    "MAD_SIGN",
    "Window",
    "Scope",
    "Series",
    "Attribution",
    "Anomaly",
    "SeriesLoader",
    "SilentFailureLoader",
    "SilentFailureRecord",
    "AnomalyDetector",
    "SilentFailureDetector",
    "MetricDailySeriesProvider",
]

logger = logging.getLogger("dba.modules.observability.anomaly")

#: 平台自身模块（03/10）——异常检测与成本曲线**必须**排除，否则自观测污染（P1-4）
SELF_MODULES: tuple[str, ...] = ("observability", "finops")

#: MAD → 标准差的换算系数（正态分布下 MAD ≈ 0.6745σ 的倒数）
MAD_SIGN: float = 0.6745


@dataclass(frozen=True, slots=True)
class Window:
    """时间窗（左闭右开，UTC）。"""

    since: datetime
    until: datetime


@dataclass(frozen=True, slots=True)
class Scope:
    """异常归属范围（与 ``alert_event.scope_type`` 对齐）。"""

    type: str = "GLOBAL"  # BIZ_LINE | AGENT | GLOBAL
    id: str | None = None

    @property
    def as_text(self) -> str:
        """``AGENT:ag_x`` / ``BIZ_LINE:12`` / ``GLOBAL``（日志与告警文案用）。"""
        return f"{self.type}:{self.id}" if self.id is not None else self.type


@dataclass(slots=True)
class Series:
    """一条待检序列（时间升序）。

    ``breakdown`` 是「自主归因」的数据来源：维度 → {取值: 增量}。例如::

        {"model": {"gpt-4o": 3_900_000}, "agent": {"ag_x": 4_100_000},
         "skill": {"dashboard.retail_daily": 27}}

    维度增量与序列同口径（成本序列 → 微美元；调用次数序列 → 次数）。
    """

    metric: str
    scope: Scope
    values: list[float]
    ts_ms: list[int] = field(default_factory=list)
    breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    trace_id: str | None = None

    @property
    def latest(self) -> float:
        """最近一个观测值（无数据时为 0.0）。"""
        return self.values[-1] if self.values else 0.0

    @property
    def has_history(self) -> bool:
        """是否有足够历史做基线（至少 2 个点：1 个历史 + 1 个待检）。"""
        return len(self.values) >= 2


@dataclass(frozen=True, slots=True)
class Attribution:
    """★ 自主归因：这次突增是谁贡献的（按 model / agent / skill 三维拆解）。

    ★ U14：归因的**权威源**是 Mongo ``anomaly_report``（存完整 attribution）；
    MySQL ``alert_event.attribution_json`` 只存**展示摘要**（``display_summary()``）。
    照抄 v1 只写一份会丢掉 hypothesis 与 skill reruns 明细，事后无法复现结论。
    """

    by_model: list[dict[str, Any]] = field(default_factory=list)
    by_agent: list[dict[str, Any]] = field(default_factory=list)
    by_skill: list[dict[str, Any]] = field(default_factory=list)
    hypothesis: str = ""

    def display_summary(self) -> dict[str, Any]:
        """MySQL ``alert_event.attribution_json`` 的**展示摘要**（可读、够用即可）。"""
        return {
            "top_model": self.by_model[0]["model"] if self.by_model else None,
            "top_agent": self.by_agent[0]["agent_uid"] if self.by_agent else None,
            "top_skill": self.by_skill[0]["skill_key"] if self.by_skill else None,
            "hypothesis": self.hypothesis,
        }

    def as_doc(self) -> dict[str, Any]:
        """Mongo ``anomaly_report.attribution`` 的**权威全文**。"""
        return {
            "by_model": list(self.by_model),
            "by_agent": list(self.by_agent),
            "by_skill": list(self.by_skill),
            "hypothesis": self.hypothesis,
        }


@dataclass(frozen=True, slots=True)
class Anomaly:
    """一条异常（会被翻译成 MySQL ``alert_event`` + Mongo ``anomaly_report``）。"""

    metric: str
    scope: Scope
    category: str  # cost_spike | fail_rate_up | skill_rot | silent_failure | loop_suspect
    severity: str  # info | warn | critical
    observed: float
    baseline: float
    robust_zscore: float
    attribution: Attribution = field(default_factory=Attribution)
    trace_id: str | None = None

    def evidence(self) -> dict[str, Any]:
        """``anomaly_report.evidence[]`` 的单条证据。"""
        return {
            "metric": self.metric,
            "window": "auto",
            "value": self.observed,
            "baseline": self.baseline,
            "robust_zscore": self.robust_zscore,
            "method": "EWMA_MAD",
        }


#: 序列加载器：``(window, scope, *, exclude_modules) -> list[Series]``
SeriesLoader = Callable[..., Awaitable[list[Series]]]


class SilentFailureLoader(Protocol):
    """静默失败明细加载器（返回候选记录，由检测器判定）。"""

    async def __call__(self, window: Window) -> list[SilentFailureRecord]: ...


@dataclass(frozen=True, slots=True)
class SilentFailureRecord:
    """一条 Run 级记录（供静默失败判定）。"""

    trace_id: str
    module: str
    status: str
    tokens: int
    tool_calls: int
    latency_ms: int
    p5_latency_ms: int | None = None
    agent_uid: str | None = None
    biz_line_id: int | None = None


def _metric_category(metric: str) -> str:
    """指标名 → ``alert_event.category``（无阈值检测也要落到可检索的分类）。"""
    name = metric.lower()
    if "silent" in name:
        return "silent_failure"
    if "fail" in name or "error" in name:
        return "fail_rate_up"
    if "skill" in name or "reuse" in name:
        return "skill_rot"
    if "loop" in name:
        return "loop_suspect"
    return "cost_spike"


def _severity_for(robust_z: float) -> str:
    """按稳健偏差幅度定严重度（仍是「相对自身基线」，不是固定阈值）。"""
    magnitude = abs(robust_z)
    if magnitude >= 8.0:
        return "critical"
    if magnitude >= 5.0:
        return "warn"
    return "info"


class AnomalyDetector:
    """★ 无阈值异常检测器（EWMA + MAD）。

    ``loader`` 为数据来源（默认不加载任何数据——数据来源由 L5 owner 的读接口提供，
    见 ``agents/anomaly.py`` 的装配）。这样检测算法本身零存储依赖、可纯内存单测。
    """

    def __init__(
        self,
        *,
        alpha: float = 0.3,
        k: float = 3.5,
        window: int = 288,
        loader: SeriesLoader | None = None,
        exclude_modules: tuple[str, ...] = SELF_MODULES,
    ) -> None:
        self.alpha = alpha
        self.k = k
        self.window = window  # 窗口点数（288 = 5 分钟粒度 × 24h）
        self._loader = loader
        self.exclude_modules = exclude_modules

    # ── 算法（纯函数，便于单测）──────────────────────────────────────
    def baseline(self, values: list[float]) -> tuple[float, float]:
        """返回 ``(EWMA 基线, MAD)``；**只用历史窗口**（不含待检的末点）。

        历史为空/只有一个点时 MAD 定义为 0（detector 会据此跳过，不误报）。
        """
        if len(values) < 2:
            return (values[-1] if values else 0.0, 0.0)
        hist = values[:-1]
        base = hist[0]
        for value in hist[1:]:
            base = self.alpha * value + (1.0 - self.alpha) * base
        median = statistics.median(hist)
        mad = statistics.median([abs(x - median) for x in hist])
        return base, mad

    def attribute(self, series: Series) -> Attribution:
        """★ 自主归因：按 model / agent / skill 三维拆解增量，取贡献最大的前几项。"""
        by_model = self._rank(series.breakdown.get("model", {}), key_name="model")
        by_agent = self._rank(series.breakdown.get("agent", {}), key_name="agent_uid")
        by_skill = self._rank_reruns(series.breakdown.get("skill", {}))
        return Attribution(
            by_model=by_model,
            by_agent=by_agent,
            by_skill=by_skill,
            hypothesis=self._hypothesis(series, by_model, by_agent, by_skill),
        )

    @staticmethod
    def _rank(contrib: dict[str, float], *, key_name: str) -> list[dict[str, Any]]:
        total = sum(abs(v) for v in contrib.values()) or 1.0
        ranked = sorted(contrib.items(), key=lambda kv: abs(kv[1]), reverse=True)
        return [
            {key_name: key, "delta_micro_usd": int(value), "share": round(abs(value) / total, 4)}
            for key, value in ranked[:3]
        ]

    @staticmethod
    def _rank_reruns(contrib: dict[str, float]) -> list[dict[str, Any]]:
        ranked = sorted(contrib.items(), key=lambda kv: abs(kv[1]), reverse=True)
        return [{"skill_key": key, "reruns": int(value)} for key, value in ranked[:3]]

    @staticmethod
    def _hypothesis(
        series: Series,
        by_model: list[dict[str, Any]],
        by_agent: list[dict[str, Any]],
        by_skill: list[dict[str, Any]],
    ) -> str:
        """给出一句可读假设（不宣称因果，只描述最强相关项）。"""
        parts = [f"指标 {series.metric} 在 {series.scope.as_text} 出现突增"]
        if by_agent:
            parts.append(f"主要贡献 Agent={by_agent[0]['agent_uid']}")
        if by_model:
            parts.append(f"主要贡献模型={by_model[0]['model']}")
        if by_skill:
            parts.append(
                f"技能 {by_skill[0]['skill_key']} 疑似参数未收敛导致重复调用 "
                f"{by_skill[0]['reruns']} 次"
            )
        return "；".join(parts)

    # ── 主流程 ───────────────────────────────────────────────────────
    async def detect(self, window: Window, scope: Scope) -> list[Anomaly]:
        """扫描窗口内的所有序列，返回异常列表（可能为空）。"""
        series_list = await self._load_series(window, scope, exclude_modules=self.exclude_modules)
        anomalies: list[Anomaly] = []
        for series in series_list:
            if not series.has_history:
                continue
            base, mad = self.baseline(series.values)
            if mad <= 0:
                continue  # 序列无波动，不判异常
            z = MAD_SIGN * (series.latest - base) / mad
            if abs(z) <= self.k:
                continue
            anomalies.append(
                Anomaly(
                    metric=series.metric,
                    scope=series.scope,
                    category=_metric_category(series.metric),
                    severity=_severity_for(z),
                    observed=round(series.latest, 6),
                    baseline=round(base, 6),
                    robust_zscore=round(z, 4),
                    attribution=self.attribute(series),
                    trace_id=series.trace_id,
                )
            )
        return anomalies

    async def _load_series(
        self,
        window: Window,
        scope: Scope,
        *,
        exclude_modules: tuple[str, ...] = SELF_MODULES,
    ) -> list[Series]:
        """★ P1-4：加载序列时**必须**排除平台自身模块。"""
        if self._loader is None:
            return []
        return await self._loader(window, scope, exclude_modules=exclude_modules)


class SilentFailureDetector:
    """★ 静默失败检测：``status=success`` 但 ``tokens=0`` 且 ``tool_calls=0``，
    或 Run 时长显著低于该类任务的 P5（典型「跑了但什么都没做」）。

    这类失败不报错、不产出，是最难发现的一种——所以单独成一类检测器。
    """

    def __init__(self, *, loader: SilentFailureLoader | None = None) -> None:
        self._loader = loader

    @staticmethod
    def is_silent(record: SilentFailureRecord) -> bool:
        """判据一：成功但零 token 零工具调用。"""
        return record.status == "success" and record.tokens == 0 and record.tool_calls == 0

    @staticmethod
    def is_below_p5(record: SilentFailureRecord) -> bool:
        """判据二：耗时显著低于同类任务 P5（须有 P5 基准才判）。"""
        return (
            record.p5_latency_ms is not None
            and record.p5_latency_ms > 0
            and record.latency_ms < record.p5_latency_ms
        )

    async def detect(self, window: Window) -> list[Anomaly]:
        if self._loader is None:
            return []
        records = await self._loader(window)
        anomalies: list[Anomaly] = []
        for record in records:
            if not (self.is_silent(record) or self.is_below_p5(record)):
                continue
            scope = (
                Scope("BIZ_LINE", str(record.biz_line_id))
                if record.biz_line_id is not None
                else Scope("GLOBAL")
            )
            anomalies.append(
                Anomaly(
                    metric="silent_fail_count",
                    scope=scope,
                    category="silent_failure",
                    severity="warn",
                    observed=1.0,
                    baseline=0.0,
                    robust_zscore=0.0,
                    attribution=Attribution(
                        hypothesis=f"Run {record.trace_id} 成功但零产出（tokens=0 且 tool_calls=0）"
                    ),
                    trace_id=record.trace_id,
                )
            )
        return anomalies


class MetricDailySeriesProvider:
    """从 ``metric_daily`` 构造待检序列（生产默认 loader）。

    ★ 关于 ``exclude_modules``：``metric_daily`` 本身**没有** module 列——平台自身模块的
    排除发生在**上游 rollup**（见 ``rollup.RollupService``，P1-4）。因此本 provider 忽略该
    参数是正确的：它读到的数据已经不含 03/10 自身。若将来有人把模块维度加进 ``metric_daily``，
    这里需要重新评估。
    """

    def __init__(
        self,
        metric_repo: Any,
        *,
        metrics: tuple[str, ...] = ("cost_micro_usd", "fail_count", "silent_fail_count"),
    ) -> None:
        self._metric = metric_repo
        self._metrics = metrics

    async def __call__(
        self,
        window: Window,
        scope: Scope,
        *,
        exclude_modules: tuple[str, ...] = SELF_MODULES,
    ) -> list[Series]:
        _ = exclude_modules  # 见类 docstring：排除已在上游 rollup 完成
        if self._metric is None:
            return []
        biz_line_id = int(scope.id) if scope.type == "BIZ_LINE" and scope.id else None
        try:
            rows = await self._metric.timeseries(
                {
                    "since": window.since.date(),
                    "until": window.until.date(),
                    "biz_line_id": biz_line_id,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 读取失败即无序列，不误报
            logger.warning("metric_daily 读取失败（异常检测降级为空）：%s", exc)
            return []

        series_list: list[Series] = []
        for metric in self._metrics:
            rows_sorted = sorted(rows, key=lambda r: str(r.get("stat_date")))
            values = [float(r.get(metric) or 0) for r in rows_sorted]
            ts = [int(r.get("stat_date").toordinal()) for r in rows_sorted] if rows_sorted else []
            if len(values) < 2:
                continue
            latest_row = rows_sorted[-1]
            breakdown = {
                "model": {str(latest_row.get("model") or ""): values[-1]},
                "agent": {str(latest_row.get("agent_uid") or ""): values[-1]},
                "skill": {},
            }
            series_list.append(
                Series(metric=metric, scope=scope, values=values, ts_ms=ts, breakdown=breakdown)
            )
        return series_list
