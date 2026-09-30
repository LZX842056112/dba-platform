"""B5 行为验证：无阈值异常告警（DoD#2）/ 复用率-成本曲线对照组（DoD#3）/ SSE seq 不丢不重（DoD#6）。

均为纯逻辑 + 替身，不依赖真实存储；用于把三条「可验证产出」固化为回归。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from dba.api.v1.stream import build_sse_frames
from dba.capabilities.messaging import ProgressPublisher
from dba.di import InMemoryProgressWriter
from dba.modules.finops.cost_curve import CostCurveAnalyzer, DailyPoint
from dba.modules.observability.agents.anomaly import AnomalyScanAgent
from dba.modules.observability.anomaly import (
    AnomalyDetector,
    Scope,
    Series,
    Window,
)
from dba_runtime.context import RunContext, reset_current, set_current


# ── DoD#2：未配置任何阈值也能产出 alert_event + attribution_json ──────
class _AlertRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, row: dict[str, Any]) -> int:
        self.rows.append(row)
        return len(self.rows)


class _ReportRepo:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def save(self, doc: dict[str, Any]) -> None:
        self.docs.append(doc)


def _spiky_loader() -> Any:
    async def _load(
        window: Window, scope: Scope, *, exclude_modules: tuple[str, ...] = ()
    ) -> list[Series]:
        _ = (window, scope, exclude_modules)
        # 历史有波动（MAD>0），末点突增 → 无阈值检测必然命中
        history = [100.0, 110.0, 90.0, 105.0, 95.0, 100.0, 108.0, 92.0, 100.0, 100.0]
        return [
            Series(
                metric="cost_micro_usd",
                scope=Scope("BIZ_LINE", "7"),
                values=[*history, 5000.0],
                breakdown={
                    "model": {"gpt-4o": 3900.0, "qwen": 100.0},
                    "agent": {"ag_dash": 4100.0},
                    "skill": {"dashboard.retail_daily": 27.0},
                },
                trace_id="f" * 32,
            )
        ]

    return _load


async def test_anomaly_no_threshold_produces_alert_with_attribution() -> None:
    alerts = _AlertRepo()
    reports = _ReportRepo()
    agent = AnomalyScanAgent(
        detector=AnomalyDetector(loader=_spiky_loader()),
        silent_detector=None,
        alert_repo=alerts,
        report_repo=reports,
    )
    run_ctx = RunContext(trace_id="a" * 32, span_id="b" * 16, module="observability")

    # ★ @traced 依赖「当前上下文」：业务代码必须由编排层驱动（此处显式包裹）
    token = set_current(run_ctx)
    try:
        output = await agent.run({"hours": 24, "biz_line_id": 7}, run_ctx)
    finally:
        reset_current(token)

    assert output.data["alert_count"] == 1
    # MySQL alert_event：可检索事件行 + 展示摘要 attribution_json
    assert len(alerts.rows) == 1
    row = alerts.rows[0]
    assert row["category"] == "cost_spike"
    assert row["severity"] == "critical"  # robust_z 幅度极大
    assert row["scope_type"] == "BIZ_LINE"
    assert row["observed_value"] == 5000.0
    assert row["attribution_json"]["top_model"] == "gpt-4o"
    assert row["attribution_json"]["top_agent"] == "ag_dash"
    assert "gpt-4o" in row["attribution_json"]["hypothesis"]
    # ★ U14：Mongo anomaly_report 存完整归因（权威源）
    assert len(reports.docs) == 1
    doc = reports.docs[0]
    assert doc["alert_id"] == 1
    assert doc["attribution"]["by_model"][0]["model"] == "gpt-4o"
    assert doc["attribution"]["by_skill"][0]["skill_key"] == "dashboard.retail_daily"
    assert doc["evidence"][0]["method"] == "EWMA_MAD"


# ── DoD#3：复用率-成本曲线带对照组 + 相关系数 ─────────────────────────
async def test_cost_curve_has_control_group_and_correlation() -> None:
    base = date(2026, 9, 1)
    points: list[DailyPoint] = []
    for i in range(7):
        points.append(
            DailyPoint(
                day=base + timedelta(days=i),
                reuse_rate=round(0.2 + 0.1 * i, 4),  # 复用率单调上升
                tokens_hit=float(1000 - 80 * i),  # 命中组 token 下降
                tokens_miss=1000.0,  # 对照组基本持平
                hit_samples=20,
                miss_samples=10,
                saved_micro_usd=1000,
            )
        )
    data = CostCurveAnalyzer().analyze(7, 30, points)

    groups = {row["group"] for row in data.token_series}
    assert groups == {"hit", "miss"}  # ★ 必须有对照组
    assert data.correlation is not None and data.correlation < 0  # 复用率↑ → token↓（负相关）
    assert data.total_saved_micro_usd == 7000
    assert "对照组" in data.note


async def test_cost_curve_no_data_does_not_fabricate() -> None:
    data = CostCurveAnalyzer().analyze(7, 30, [])
    assert data.correlation is None  # 样本不足返回 None，不编造
    assert data.token_series == []
    assert "无数据" in data.note


# ── DoD#6：SSE 回放 seq 不丢不重 ──────────────────────────────────────
async def test_sse_replay_preserves_seq_no_loss_no_dup() -> None:
    publisher = ProgressPublisher(InMemoryProgressWriter())
    for i in range(5):
        await publisher.publish("t" * 32, "run:progress", {"step": i})

    # 首连：回放全部 5 条，seq 严格为 1..5
    all_events = await publisher.replay("t" * 32)
    assert [e.seq for e in all_events] == [1, 2, 3, 4, 5]

    frames_all = build_sse_frames(all_events)
    assert frames_all[0].startswith("id: 1\n")
    assert all("id: " in f for f in frames_all)

    # 断线重连：Last-Event-ID=3 → 只补发 seq>3 的两条（不丢不重）
    resumed = await publisher.replay("t" * 32, after_seq=3)
    assert [e.seq for e in resumed] == [4, 5]
    frames_resumed = build_sse_frames(resumed)
    assert len(frames_resumed) == 2
    assert frames_resumed[0].startswith("id: 4\n")
    assert frames_resumed[1].startswith("id: 5\n")


async def test_sse_seq_monotonic_across_many_publishes() -> None:
    publisher = ProgressPublisher(InMemoryProgressWriter())
    for i in range(20):
        event = await publisher.publish("u" * 32, "run:progress", {"i": i})
        assert event.seq == i + 1
    replayed = await publisher.replay("u" * 32)
    seqs = [e.seq for e in replayed]
    assert seqs == sorted(seqs)  # 单调
    assert len(seqs) == len(set(seqs)) == 20  # 不重
