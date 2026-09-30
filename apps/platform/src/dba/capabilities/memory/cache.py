"""记忆服务（两级缓存：精确 + 语义）。

对齐《设计文档 v2》§6.6 / §5.4 / §5.3 与《实现要点清单》§6.6。

★ 两级缓存与**权限校验**（照抄 v1 会怎样错）
------------------------------------------
v1 只按 ``query_hash`` 命中缓存，**不带** ``biz_line_id`` 与 ``scope_hash``：
A 业务线用户查「本月营收」缓存了答案，B 业务线/权限更小的用户查同一句话会**直接命中
A 的答案** → 越权泄漏。

v2 修法：
* 缓存唯一键 ``(query_hash, biz_line_id, scope_hash)``（§5.4）；
* L2 语义命中也**必须回 Mongo ``semantic_cache_entry`` 校验 scope_hash**（Milvus 只负责召回，
  不承担权限判定）；不匹配则丢弃并回源；
* ``dba_semantic_cache_vec`` 检索还必须叠加 ``ttl_epoch > now``（过期缓存不得命中）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from dba.storage.milvus.collections import COL_SEMANTIC_CACHE

__all__ = ["MemoryService", "MemoryHit", "QueryCacheKey", "normalize_query", "query_hash"]

logger = logging.getLogger("dba.capabilities.memory")


def normalize_query(text: str) -> str:
    """查询归一化（去首尾空白、折叠内部空白、统一小写）。

    归一化后才能做「精确缓存」；否则大小写/空格差异会让同义查询错失缓存。
    """
    return " ".join(text.strip().lower().split())


def query_hash(text: str) -> str:
    """``sha256(normalize_query(text))[:32]``。"""
    return hashlib.sha256(normalize_query(text).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class QueryCacheKey:
    """缓存唯一键（§5.4 唯一索引三件套）。"""

    query_hash: str
    biz_line_id: int
    scope_hash: str

    @classmethod
    def of(cls, text: str, biz_line_id: int, scope_hash: str) -> QueryCacheKey:
        return cls(query_hash(text), biz_line_id, scope_hash)


@dataclass
class MemoryHit:
    """一次缓存命中。"""

    answer: dict[str, Any]
    source: str  # "l1"（精确） | "l2"（语义）
    score: float = 1.0
    key: QueryCacheKey | None = None


class MemoryService:
    """两级记忆服务（owner: capabilities.memory）。

    只依赖 Protocol（``SemanticCacheEntryRepo`` / ``VectorRepo``），不 import 具体实现。
    """

    #: 语义命中最低相似度（COSINE）
    SEMANTIC_MIN_SCORE = 0.92

    def __init__(
        self,
        *,
        cache_repo: Any,
        vector_repo: Any,
        ttl_s: int = 3600,
    ) -> None:
        self._cache = cache_repo
        self._vector = vector_repo
        self._ttl_s = ttl_s
        #: 自监控计数（导出 mem_lookup / mem_hit）
        self.lookup_count = 0
        self.hit_count = 0

    async def lookup(
        self,
        text: str,
        *,
        biz_line_id: int,
        scope_hash: str,
        vector: list[float] | None = None,
    ) -> MemoryHit | None:
        """两级查找。返回命中（含来源）或 ``None``。"""
        self.lookup_count += 1
        key = QueryCacheKey.of(text, biz_line_id, scope_hash)

        # L1：精确命中（唯一键三件套完全一致）
        exact = await self._cache.get(key.query_hash, biz_line_id, scope_hash)
        if exact is not None and self._is_fresh(exact):
            self.hit_count += 1
            return MemoryHit(
                answer=dict(exact.get("answer") or {}), source="l1", score=1.0, key=key
            )

        # L2：语义召回（Milvus），**回 Mongo 校验 scope_hash** 后才算命中
        if vector is None:
            return None
        try:
            hits = await self._vector.search(
                COL_SEMANTIC_CACHE,
                vector,
                expr=f'biz_line_id == {biz_line_id} and scope_hash == "{scope_hash}"',
                limit=5,
                output_fields=["query_hash", "biz_line_id", "scope_hash"],
            )
        except Exception:  # noqa: BLE001 - 向量库可降级：失败即视为未命中
            logger.warning("语义缓存检索失败，降级为未命中", exc_info=True)
            return None

        for hit in hits:
            if float(hit.get("score", 0.0)) < self.SEMANTIC_MIN_SCORE:
                continue
            qh = str(hit.get("query_hash") or "")
            if not qh:
                continue
            # ★ 必须回权威集校验 scope_hash（Milvus 不承担权限判定）
            entry = await self._cache.get(qh, biz_line_id, scope_hash)
            if entry is None or not self._is_fresh(entry):
                continue
            self.hit_count += 1
            return MemoryHit(
                answer=dict(entry.get("answer") or {}),
                source="l2",
                score=float(hit.get("score", 0.0)),
                key=QueryCacheKey(qh, biz_line_id, scope_hash),
            )
        return None

    async def put(
        self,
        text: str,
        *,
        biz_line_id: int,
        scope_hash: str,
        answer: dict[str, Any],
        vector: list[float] | None = None,
        ttl_s: int | None = None,
    ) -> QueryCacheKey:
        """写入缓存（L1 权威集 + L2 向量）。"""
        key = QueryCacheKey.of(text, biz_line_id, scope_hash)
        ttl = ttl_s if ttl_s is not None else self._ttl_s
        now = dt.datetime.now(dt.UTC)
        expire_at = now + dt.timedelta(seconds=ttl)
        ttl_epoch = int(expire_at.timestamp())

        await self._cache.upsert(
            {
                "query_hash": key.query_hash,
                "biz_line_id": biz_line_id,
                "scope_hash": scope_hash,
                "query_text": normalize_query(text),
                "answer": answer,
                "created_at": now.replace(tzinfo=None),
                "expire_at": expire_at.replace(tzinfo=None),
                "ttl_epoch": ttl_epoch,
            }
        )
        if vector is not None:
            try:
                await self._vector.upsert(
                    COL_SEMANTIC_CACHE,
                    [
                        {
                            "vector": vector,
                            "query_hash": key.query_hash,
                            "biz_line_id": biz_line_id,
                            "scope_hash": scope_hash,
                            "ttl_epoch": ttl_epoch,
                        }
                    ],
                )
            except Exception:  # noqa: BLE001 - 向量写入失败不影响精确缓存
                logger.warning("语义缓存写入失败（精确缓存已生效）", exc_info=True)
        return key

    async def purge_expired(self) -> int:
        """清理过期缓存条目（worker 定时调用）。"""
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        deleted = int(await self._cache.delete_expired(now))
        try:
            await self._vector.delete_by_expr(
                COL_SEMANTIC_CACHE, f"ttl_epoch < {int(now.timestamp())}"
            )
        except Exception:  # noqa: BLE001
            logger.warning("清理过期向量失败", exc_info=True)
        return deleted

    @property
    def hit_rate(self) -> float:
        if self.lookup_count == 0:
            return 0.0
        return self.hit_count / self.lookup_count

    @staticmethod
    def _is_fresh(entry: dict[str, Any]) -> bool:
        ttl_epoch = entry.get("ttl_epoch")
        if ttl_epoch is None:
            return True
        return int(ttl_epoch) > int(dt.datetime.now(dt.UTC).timestamp())
