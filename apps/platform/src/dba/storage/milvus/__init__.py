"""Milvus 向量存储适配层（L5）：5 个 collection（§5.3）。

★ 分区键统一用 ``biz_line_id``（**哨兵 0**，与 MySQL ``metric_daily`` 一致，P1-1）；
★ HNSW 参数 M=16 / efConstruction=200，度量 COSINE（§5.3）；
★ ``dba_semantic_cache_vec`` 检索**必须叠加 ``ttl_epoch > now``**，否则命中已过期缓存。
"""

from __future__ import annotations

from .client import MilvusStorage, build_milvus
from .collections import COLLECTION_SPECS, CollectionSpec
from .repo import VectorRepo

__all__ = [
    "COLLECTION_SPECS",
    "CollectionSpec",
    "MilvusStorage",
    "VectorRepo",
    "build_milvus",
]
