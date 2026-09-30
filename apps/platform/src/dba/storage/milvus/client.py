"""Milvus 客户端封装（pymilvus，懒加载 + 线程池桥接）。

★ 为什么用 ``asyncio.to_thread``：pymilvus 是**同步阻塞**客户端；在 async 服务里直接调用
会阻塞事件循环。本层把每次调用丢到线程池执行，保持 L4/L3 的 async 契约。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dba_runtime.errors import StorageUnavailableError

from .collections import COLLECTION_SPECS, CollectionSpec

__all__ = ["MilvusStorage"]

logger = logging.getLogger("dba.storage.milvus")

_COLLECTION_BY_NAME: dict[str, CollectionSpec] = {s.name: s for s in COLLECTION_SPECS}


def build_milvus(host: str, port: int) -> Any:
    try:
        from pymilvus import connections  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "Milvus 需要安装 `dba[milvus]`（pymilvus）", detail={"component": "milvus"}
        ) from exc
    alias = "dba"
    connections.connect(alias=alias, host=host, port=str(port))
    return alias


class MilvusStorage:
    """Milvus 存储聚合：连接 + 集合适配（ensure / upsert / search / delete）。"""

    def __init__(self, host: str, port: int, *, dim: int = 1024) -> None:
        self._host = host
        self._port = port
        self._dim = dim
        self._alias: str | None = None

    def _ensure_alias(self) -> str:
        if self._alias is None:
            self._alias = build_milvus(self._host, self._port)
        return self._alias

    async def ping(self) -> bool:
        try:
            await asyncio.to_thread(self._ensure_alias)
            from pymilvus import utility  # noqa: PLC0415

            await asyncio.to_thread(utility.list_collections, using=self._alias)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def ensure_collections(self) -> None:
        await asyncio.to_thread(self._ensure_collections_sync)

    def _ensure_collections_sync(self) -> None:
        alias = self._ensure_alias()
        from pymilvus import (  # noqa: PLC0415
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            utility,
        )

        existing = set(utility.list_collections(using=alias))
        for spec in COLLECTION_SPECS:
            if spec.name in existing:
                continue
            fields = [
                FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self._dim),
            ]
            for fname, ftype in spec.scalar_fields:
                dtype = getattr(DataType, ftype)
                fields.append(
                    FieldSchema(name=fname, dtype=dtype, max_length=256)
                    if ftype == "VARCHAR"
                    else FieldSchema(name=fname, dtype=dtype)
                )
            if spec.has_ttl:
                fields.append(FieldSchema(name="ttl_epoch", dtype=DataType.INT64))
            # ★ 分区键统一 biz_line_id（哨兵 0），实现按业务线物理隔离
            schema = CollectionSchema(
                fields=fields, description=spec.description, partition_key_field="biz_line_id"
            )
            coll = Collection(name=spec.name, schema=schema, using=alias)
            coll.create_index(
                field_name="vector",
                index_params={
                    "index_type": spec.index_type,
                    "metric_type": spec.metric_type,
                    "params": {"M": spec.hnsw_m, "efConstruction": spec.hnsw_ef_construction},
                },
            )
            coll.load()

    async def upsert(self, collection: str, rows: list[dict[str, Any]]) -> int:
        return int(await asyncio.to_thread(self._upsert_sync, collection, rows))

    def _upsert_sync(self, collection: str, rows: list[dict[str, Any]]) -> int:
        from pymilvus import Collection  # noqa: PLC0415

        coll = Collection(name=collection, using=self._ensure_alias())
        spec = _COLLECTION_BY_NAME[collection]
        fields = ["vector", *(n for n, _ in spec.scalar_fields)]
        if spec.has_ttl:
            fields.append("ttl_epoch")
        data = [[row.get(f) for row in rows] for f in fields]
        coll.upsert(data)
        coll.flush()
        return len(rows)

    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        expr: str,
        limit: int = 8,
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._search_sync, collection, vector, expr, limit, output_fields
        )

    def _search_sync(
        self,
        collection: str,
        vector: list[float],
        expr: str,
        limit: int,
        output_fields: list[str] | None,
    ) -> list[dict[str, Any]]:
        from pymilvus import Collection  # noqa: PLC0415

        spec = _COLLECTION_BY_NAME[collection]
        coll = Collection(name=collection, using=self._ensure_alias())
        coll.load()
        # ★ 缓存类 collection 强制叠加 ttl_epoch 过滤（调用方传入 expr 必须已含）
        results = coll.search(
            data=[vector],
            anns_field="vector",
            param={"metric_type": spec.metric_type, "params": {"ef": 128}},
            limit=limit,
            expr=expr or None,
            output_fields=output_fields or ["id"],
        )
        out: list[dict[str, Any]] = []
        for hit in results[0]:
            item: dict[str, Any] = {"id": hit.id, "score": float(hit.score)}
            entity = getattr(hit, "entity", None)
            if entity is not None and output_fields:
                for f in output_fields:
                    item[f] = entity.get(f)
            out.append(item)
        return out

    async def delete_by_expr(self, collection: str, expr: str) -> int:
        return int(await asyncio.to_thread(self._delete_sync, collection, expr))

    def _delete_sync(self, collection: str, expr: str) -> int:
        from pymilvus import Collection  # noqa: PLC0415

        coll = Collection(name=collection, using=self._ensure_alias())
        result = coll.delete(expr)
        coll.flush()
        return int(getattr(result, "delete_count", 0))

    async def aclose(self) -> None:
        if self._alias is None:
            return
        try:
            from pymilvus import connections  # noqa: PLC0415

            await asyncio.to_thread(connections.disconnect, self._alias)
        except Exception:  # noqa: BLE001
            logger.warning("断开 Milvus 连接失败", exc_info=True)
