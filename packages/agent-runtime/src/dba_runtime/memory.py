"""记忆客户端（内核 L2，◆建议签名）。

对齐《设计方案 v2》§2.4 / §6.2 / §5.9 与《实现要点清单》§5.6、U1。

★ U1：文档只声明「``MemoryClient`` Protocol」，正文无方法签名；此处按调用点反推并冻结。
owner: ``MemoryService``（读 MySQL ``sem_*`` + Milvus collection）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .context import RunContext

__all__ = ["RecallResult", "CacheHit", "MemoryClient"]


class RecallResult(BaseModel):
    """语义召回结果（口径 / 字段映射 / 既有大屏骨架）。"""

    hits: list[dict[str, Any]] = Field(default_factory=list)
    lookups: int = 0
    scope_hash: str | None = None

    @property
    def hit_count(self) -> int:
        return len(self.hits)


class CacheHit(BaseModel):
    """语义缓存命中。``result_ref`` 指向权威结果（大屏 / 结果对象键）。"""

    query_hash: str
    scope_hash: str
    biz_line_id: int | None = None
    result_ref: dict[str, Any] = Field(default_factory=dict)
    ttl_epoch: int | None = None


@runtime_checkable
class MemoryClient(Protocol):
    """owner: MemoryService。读 MySQL sem_* + Milvus collection。"""

    async def recall(self, question: str, ctx: RunContext) -> RecallResult: ...

    async def cache_lookup(self, question: str, ctx: RunContext) -> CacheHit | None:
        """★ 必须带 ``scope_hash`` 校验：命中条目与当前权限指纹不一致时视为未命中。"""
        ...

    async def cache_write(self, question: str, result_ref: dict[str, Any], ctx: RunContext) -> None:
        """写入语义缓存元数据（含 ``scope_hash`` + ``ttl_epoch``）。"""
        ...
