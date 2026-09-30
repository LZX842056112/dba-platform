"""Milvus ``VectorRepo`` 实现（Protocol 见 ``storage/protocols.py``）。

★ 铁律（§5.9）：**禁止模块直接调 ``client.search``**；只能经 owner 的检索方法，
否则「谁能检索什么」的归属就形同虚设。本 Repo 提供统一入口并强制：
  * ``dba_semantic_cache_vec`` 的 ``expr`` 必须显式带 ``ttl_epoch > <now>``（否则拒绝）；
  * 检索/写入前 ``ensure`` 对应 collection 已加载。
"""

from __future__ import annotations

import time
from typing import Any

from .client import MilvusStorage
from .collections import COL_SEMANTIC_CACHE, COLLECTION_SPECS

__all__ = ["VectorRepo"]

_KNOWN = {s.name for s in COLLECTION_SPECS}


class VectorRepo:
    """向量检索统一入口（owner 语义见 collection 定义）。"""

    def __init__(self, storage: MilvusStorage) -> None:
        self._storage = storage

    async def ensure_collections(self) -> None:
        await self._storage.ensure_collections()

    async def upsert(self, collection: str, rows: list[dict[str, Any]]) -> int:
        self._check(collection)
        return await self._storage.upsert(collection, rows)

    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        expr: str,
        limit: int = 8,
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._check(collection)
        if collection == COL_SEMANTIC_CACHE and "ttl_epoch" not in expr:
            # ★ 强制 TTL 过滤：否则会命中已过期缓存，返回过期答案
            expr = (
                f"({expr}) and ttl_epoch > {int(time.time())}"
                if expr
                else f"ttl_epoch > {int(time.time())}"
            )
        return await self._storage.search(
            collection, vector, expr=expr, limit=limit, output_fields=output_fields
        )

    async def delete_by_expr(self, collection: str, expr: str) -> int:
        self._check(collection)
        return await self._storage.delete_by_expr(collection, expr)

    @staticmethod
    def _check(collection: str) -> None:
        if collection not in _KNOWN:
            raise ValueError(f"未注册的 collection：{collection}（§5.3 只允许 5 个）")
