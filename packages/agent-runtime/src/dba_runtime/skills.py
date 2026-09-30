"""技能服务（内核 L2，◆建议签名）。

对齐《设计方案 v2》§2.4 / §6.2 与《实现要点清单》§5.6、U1。

★ U1：文档只声明「``SkillSpec`` / ``SkillService`` Protocol」，正文无方法签名；此处按调用点
反推并冻结。owner: ``SkillService``（``skill_registry`` / ``skill_usage`` + Mongo ``skill_def``
+ Milvus）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .context import RunContext

__all__ = ["SkillSpec", "SkillStats", "SkillService"]


class SkillSpec(BaseModel):
    """技能定义（三层结构）。"""

    skill_key: str
    version: int
    meta: dict[str, Any] = Field(default_factory=dict)  # ① 元数据层
    steps: list[dict[str, Any]] = Field(default_factory=list)  # ② 步骤层（llm|tool|sql|sub_skill）
    params_schema: dict[str, Any] = Field(default_factory=dict)  # ③ 参数层（JSON Schema）


class SkillStats(BaseModel):
    """技能统计（供 03/10 复用率与死技能判据）。"""

    total: int = 0
    reuse_rate: float = 0.0
    dead_count: int = 0
    by_skill: list[dict[str, Any]] = Field(default_factory=list)


@runtime_checkable
class SkillService(Protocol):
    """owner: SkillService（表 skill_registry/skill_usage + Mongo skill_def + Milvus）。"""

    async def match(self, intent: str, ctx: RunContext) -> SkillSpec | None:
        """按意图匹配已注册技能；未命中返回 None。"""
        ...

    async def stats(self, *, biz_line_id: int | None = None) -> SkillStats: ...

    async def record_usage(
        self, skill_key: str, ctx: RunContext, is_reuse: bool, tokens_saved_est: int = 0
    ) -> None:
        """记录技能用量。★ upsert 唯一键 ``(skill_id, trace_id)``（P1-2）。"""
        ...
