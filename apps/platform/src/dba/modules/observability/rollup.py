"""模块 03 · 指标 rollup（小时/日聚合 → ``metric_daily``）。

对齐《设计方案 v2》§6.4、§5.2.5 与《实现要点清单》§6.7、P1-1、P1-2、P2-9、
§12.6（三方对账中的「明细累加」一方）。

三条硬约束（照抄 v1 会怎样错）
------------------------------
1. **幂等（P2-9）**：按 ``(stat_date, biz_line_id, agent_uid, model)`` **全量重算**后
   ``upsert``（走 ``metric_daily`` 主键）。重跑一次不会把计数翻倍——v1 的 ``+=`` 累加
   在 job 重试时会重复计入。
2. **``avg_latency_ms`` 按 count 加权（P1-1）**：``Σlatency / Σruns``。不能对「小时均值」
   再取平均——那会让样本少的小时权重过大。``p50/p95`` 不能由聚合再聚合，须从 ES 明细
   按日回填（本批次标注 TODO，见报告「遗留问题」）。
3. **★ P1-4 排除平台自身模块**：``module ∈ {observability, finops}`` 的 Run **不进**
   业务 ``metric_daily``；否则 03 的扫描、10 的对账会把自己算进业务成本曲线。
   平台自身消耗走独立视图（``GET /obs/self-cost``，见 ``service.ObservabilityService``）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .anomaly import SELF_MODULES

__all__ = ["RollupReport", "RollupService"]

logger = logging.getLogger("dba.modules.observability.rollup")

#: ``run.status`` → metric_daily 计数列
_STATUS_COLUMNS: dict[str, str] = {
    "success": "success_count",
    "failed": "fail_count",
    "timeout": "timeout_count",
    "aborted": "fail_count",
}


@dataclass(slots=True)
class RollupReport:
    """一次 rollup 的结果（供 job 记录与排障）。"""

    stat_date: date
    rows: int = 0
    run_count: int = 0
    self_run_count: int = 0  # 被排除的平台自身 Run 数
    rollup_run_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))


class RollupService:
    """把某日 ``run`` 明细聚合为 ``metric_daily``（幂等全量重算 + upsert）。"""

    def __init__(
        self,
        *,
        run_repo: Any,
        metric_repo: Any,
        skill_metrics: Any = None,
        memory_metrics: Any = None,
        exclude_modules: tuple[str, ...] = SELF_MODULES,
    ) -> None:
        self._run_repo = run_repo
        self._metric_repo = metric_repo
        self._skill_metrics = skill_metrics
        self._memory_metrics = memory_metrics
        self.exclude_modules = exclude_modules
        #: 上次 rollup 水位（进程内；跨进程水位需 DDL 列，见报告「遗留问题」）
        self.last_report: RollupReport | None = None

    async def run(self, stat_date: date, *, biz_line_id: int | None = None) -> RollupReport:
        """执行一次 rollup。**全量重算**，重复调用结果一致（幂等）。"""
        report = RollupReport(stat_date=stat_date)
        since = datetime.combine(stat_date, datetime.min.time())
        until = since + timedelta(days=1)

        runs = await self._load_runs(since, until, biz_line_id)
        report.run_count = len(runs)

        buckets: dict[tuple[int, str, str], dict[str, int]] = defaultdict(
            lambda: {
                "run_count": 0,
                "success_count": 0,
                "fail_count": 0,
                "timeout_count": 0,
                "silent_fail_count": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cached_tokens": 0,
                "cost_micro_usd": 0,
                "latency_sum": 0,
                "llm_calls": 0,
                "tool_calls": 0,
            }
        )

        for run in runs:
            module = str(run.get("module") or "")
            if module in self.exclude_modules:
                report.self_run_count += 1
                continue
            key = (
                int(run.get("biz_line_id") or 0),  # ★ P1-1：哨兵 0 = 全局/未归属
                str(run.get("agent_uid") or ""),
                "",  # model 维度：run 表无 model，按维度不细分（模型维度由 llm_call 明细回填）
            )
            b = buckets[key]
            b["run_count"] += 1
            status = str(run.get("status") or "")
            col = _STATUS_COLUMNS.get(status)
            if col:
                b[col] += 1
            tokens_in = int(run.get("tokens_in") or 0)
            tool_calls = int(run.get("tool_calls") or 0)
            if status == "success" and tokens_in == 0 and tool_calls == 0:
                b["silent_fail_count"] += 1
            b["tokens_in"] += tokens_in
            b["tokens_out"] += int(run.get("tokens_out") or 0)
            b["cached_tokens"] += int(run.get("cached_tokens") or 0)
            b["cost_micro_usd"] += int(run.get("cost_micro_usd") or 0)
            b["latency_sum"] += int(run.get("latency_ms") or 0)
            b["llm_calls"] += int(run.get("llm_calls") or 0)
            b["tool_calls"] += tool_calls

        # ★ P1-1 修复：技能/记忆指标须**并入**全局 bucket，而非另起一行。
        #   ``_metric_rows`` 返回的全局行主键与「biz_line_id=0 的 run bucket」相同
        #   ``(0, '', '')``，若直接 append，后写的 skill 行会在 ON DUPLICATE KEY UPDATE
        #   里把 run_count/cost/tokens 覆盖成 0（实测：metric_daily.cost 恒 0 的根因）。
        for extra in await self._metric_rows(stat_date, biz_line_id):
            gkey = (
                int(extra.get("biz_line_id") or 0),
                str(extra.get("agent_uid") or ""),
                str(extra.get("model") or ""),
            )
            for col, val in extra.items():
                if col not in {"stat_date", "biz_line_id", "agent_uid", "model"}:
                    buckets[gkey][col] = val

        rows = [self._to_row(stat_date, key, b) for key, b in buckets.items()]

        if self._metric_repo is not None and rows:
            await self._metric_repo.upsert_many(rows)
        report.rows = len(rows)
        self.last_report = report
        logger.info(
            "rollup 完成 stat_date=%s rows=%d runs=%d self_excluded=%d",
            stat_date,
            report.rows,
            report.run_count,
            report.self_run_count,
        )
        return report

    async def _load_runs(
        self, since: datetime, until: datetime, biz_line_id: int | None
    ) -> list[dict[str, Any]]:
        if self._run_repo is None:
            return []
        limit = 100000
        try:
            rows: list[dict[str, Any]] = await self._run_repo.list_runs(
                {"biz_line_id": biz_line_id, "since": since, "until": until, "limit": limit},
                limit=limit,
            )
            return rows
        except Exception as exc:  # noqa: BLE001 - 读取失败降级为空（不写脏数据）
            logger.warning("rollup 读取 run 明细失败（降级为空）：%s", exc)
            return []

    @staticmethod
    def _to_row(stat_date: date, key: tuple[int, str, str], b: dict[str, int]) -> dict[str, Any]:
        """聚合成一行 ``metric_daily``（★ ``avg_latency_ms`` 按 count 加权）。"""
        biz_line_id, agent_uid, model = key
        run_count = b["run_count"]
        avg_latency = (b["latency_sum"] // run_count) if run_count else None
        # ★ 只放 metric_daily 真实存在的列：``llm_calls``/``tool_calls`` 不属于本表
        #   （它们只用于上面判定静默失败），带进来会因「未知列」导致 upsert 全表失败。
        row: dict[str, Any] = {
            "stat_date": stat_date,
            "biz_line_id": biz_line_id,
            "agent_uid": agent_uid,
            "model": model,
            "run_count": run_count,
            "success_count": b["success_count"],
            "fail_count": b["fail_count"],
            "timeout_count": b["timeout_count"],
            "silent_fail_count": b["silent_fail_count"],
            "tokens_in": b["tokens_in"],
            "tokens_out": b["tokens_out"],
            "cached_tokens": b["cached_tokens"],
            "cost_micro_usd": b["cost_micro_usd"],
            "avg_latency_ms": avg_latency,
        }
        # ★ 并入全局 bucket 的技能/记忆指标列（仅全局行携带；其余 bucket 无此键，不写）
        for col in (
            "skill_total",
            "skill_used",
            "skill_reuse_rate",
            "dead_skill_count",
            "mem_lookup",
            "mem_hit",
            "mem_hit_rate",
        ):
            if col in b:
                row[col] = b[col]
        return row

    async def _metric_rows(self, stat_date: date, biz_line_id: int | None) -> list[dict[str, Any]]:
        """技能 / 记忆指标行（挂到 ``(biz_line_id, '', '')`` 全局维度）。"""
        if self._skill_metrics is None and self._memory_metrics is None:
            return []
        row: dict[str, Any] = {
            "stat_date": stat_date,
            "biz_line_id": int(biz_line_id or 0),
            "agent_uid": "",
            "model": "",
        }
        if self._skill_metrics is not None:
            try:
                skill = await self._skill_metrics.compute(stat_date, biz_line_id)
                row.update(
                    {
                        "skill_total": skill.total,
                        "skill_used": skill.used,
                        "skill_reuse_rate": skill.reuse_rate,
                        "dead_skill_count": skill.dead_count,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("技能指标计算失败（降级）：%s", exc)
        if self._memory_metrics is not None:
            try:
                mem = await self._memory_metrics.compute(stat_date, biz_line_id)
                row.update(
                    {
                        "mem_lookup": mem.lookups,
                        "mem_hit": mem.hits,
                        "mem_hit_rate": mem.hit_rate,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("记忆指标计算失败（降级）：%s", exc)
        return [row]
