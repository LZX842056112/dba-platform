"""技能服务（★ P1-2：复用率不被灌水）。

对齐《设计文档 v2》§5.2.5 ``skill_registry``/``skill_usage`` 与《实现要点清单》P1-2。

★ P1-2（照抄 v1 会怎样错）
------------------------
SQL 生成失败后的 **self-heal 回退**会让同一次 Run 多次经过**同一个技能**。
v1 每次经过都往 ``skill_usage`` INSERT 一行 → 同一 trace 产生多条记录 →
「复用率 = reuse 次数 / 使用次数」被**灌水**（分母虚高、指标失真的同时还会误判技能死/活）。

v2 修法：``skill_usage`` 建 ``UNIQUE(skill_id, trace_id)``，写入走
``INSERT ... ON DUPLICATE KEY UPDATE``（见 ``storage/mysql/repo.py::SkillUsageRepo.record``）。
本服务另维护 ``skill_registry`` 的聚合计数（``usage_count``/``reuse_count``），
每次调用按「是否首次经过该技能」决定是否 +1，避免重复累计。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from dba.storage.milvus.collections import COL_SKILL

__all__ = ["SkillMatch", "SkillService"]

logger = logging.getLogger("dba.capabilities.skills")


@dataclass
class SkillMatch:
    """一次技能匹配结果。"""

    skill_id: int
    skill_key: str
    name: str
    score: float
    source: str  # "registry" | "vector"
    steps: list[dict[str, Any]]
    params_schema: dict[str, Any]
    version: int = 1

    @classmethod
    def from_row(cls, row: dict[str, Any], score: float, source: str) -> SkillMatch:
        steps = row.get("steps_json") or []
        params = row.get("params_schema") or {}
        if isinstance(steps, str):
            import json  # noqa: PLC0415

            steps = json.loads(steps)
        if isinstance(params, str):
            import json  # noqa: PLC0415

            params = json.loads(params)
        return cls(
            skill_id=int(row["id"]),
            skill_key=str(row["skill_key"]),
            name=str(row["name"]),
            score=score,
            source=source,
            steps=list(steps),
            params_schema=dict(params),
            version=int(row.get("version", 1)),
        )


class SkillService:
    """技能匹配 / 注册 / 使用记录（只依赖 Protocol）。"""

    #: 向量匹配最低相似度
    VECTOR_MIN_SCORE = 0.75

    def __init__(
        self,
        *,
        registry_repo: Any,
        usage_repo: Any,
        vector_repo: Any | None = None,
    ) -> None:
        self._registry = registry_repo
        self._usage = usage_repo
        self._vector = vector_repo

    async def match(
        self,
        intent: str,
        *,
        biz_line_id: int | None,
        vector: list[float] | None = None,
        limit: int = 5,
    ) -> list[SkillMatch]:
        """匹配技能：先注册表（规则/关键词），再用向量召回补充（去重）。"""
        matches: list[SkillMatch] = []
        seen: set[int] = set()

        for row in await self._registry.match_intent(intent, biz_line_id):
            match = SkillMatch.from_row(row, score=0.6, source="registry")
            if match.skill_id not in seen:
                seen.add(match.skill_id)
                matches.append(match)

        if vector is not None and self._vector is not None:
            expr = f"biz_line_id == {biz_line_id}" if biz_line_id is not None else ""
            try:
                hits = await self._vector.search(
                    COL_SKILL,
                    vector,
                    expr=expr,
                    limit=limit,
                    output_fields=["skill_key", "biz_line_id"],
                )
            except Exception:  # noqa: BLE001 - 向量库可降级
                logger.warning("技能向量召回失败，降级为仅注册表", exc_info=True)
                hits = []
            for hit in hits:
                if float(hit.get("score", 0.0)) < self.VECTOR_MIN_SCORE:
                    continue
                key = str(hit.get("skill_key") or "")
                if not key:
                    continue
                row = await self._registry.by_key(key)
                if row is None or int(row["id"]) in seen:
                    continue
                seen.add(int(row["id"]))
                matches.append(SkillMatch.from_row(row, score=float(hit["score"]), source="vector"))

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:limit]

    async def record_use(
        self,
        *,
        skill_id: int,
        trace_id: str,
        biz_line_id: int | None,
        is_reuse: bool,
        first_time_in_trace: bool = True,
        tokens_saved_est: int = 0,
        baseline_cost_micro_usd: int = 0,
    ) -> None:
        """记录一次技能使用。

        * ``is_reuse=True`` 表示复用既有技能（命中缓存/技能库），否则为新建/首次使用；
        * ``first_time_in_trace``：★ P1-2 的关键——**同一 trace 内重复经过不重复计数**；
          ``skill_usage`` 行由 ``UNIQUE(skill_id, trace_id)`` + upsert 兜底，
          ``skill_registry`` 的聚合计数改由 worker rollup 从 ``skill_usage``
          汇总（避免高频写热点）。
        """
        await self._usage.record(
            skill_id=skill_id,
            trace_id=trace_id,
            biz_line_id=biz_line_id,
            is_reuse=is_reuse,
            tokens_saved_est=tokens_saved_est,
            baseline_cost_micro_usd=baseline_cost_micro_usd,
        )

    async def register(self, definition: dict[str, Any]) -> int:
        """注册 / 更新一个技能定义，返回 ``skill_id``。"""
        return int(await self._registry.upsert(definition))

    async def stats(self, biz_line_id: int | None) -> dict[str, Any]:
        """技能统计（供 metric_daily rollup 使用）。"""
        return dict(await self._registry.stats(biz_line_id))

    async def save_definition_doc(self, skill_def_repo: Any, doc: dict[str, Any]) -> None:
        """把技能定义原文存 Mongo ``skill_def``（步骤/参数的权威文档）。"""
        await skill_def_repo.save_version(doc)
