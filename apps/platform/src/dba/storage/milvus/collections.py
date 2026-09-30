"""Milvus 集合定义（§5.3）。

★ 为什么加 ``dba_`` 前缀
----------------------
虚拟机 ``192.168.200.10`` 上同时服务着**其他项目**（已有 ``kb_item_names`` /
``kb_chunks`` / ``kb_evolution_items`` 等 collection）。本项目 5 个 collection
**统一加 ``dba_`` 前缀**，确保只在本项目命名空间内建表、绝不与邻居项目冲突。

5 个 collection 与其 owner（§5.9 归属矩阵）：
  1. ``dba_sem_metric_vec``      owner=capabilities.semantics   指标口径语义检索
  2. ``dba_skill_vec``           owner=capabilities.skills      技能意图匹配
  3. ``dba_semantic_cache_vec``  owner=capabilities.memory      语义缓存（带 ttl_epoch）
  4. ``dba_finops_kb_vec``       owner=modules.finops           降本知识库检索
  5. ``dba_obs_anomaly_vec``     owner=modules.observability    历史异常相似召回

★ 具名常量集中在此：任何地方引用 collection 名**必须**用下面的常量
  （``COL_SEM_METRIC`` 等），不要再写裸字符串 —— 否则改名只改一半，运行时才炸。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "BIZ_LINE_SENTINEL",
    "COLLECTION_SPECS",
    "COL_FINOPS_KB",
    "COL_OBS_ANOMALY",
    "COL_SEMANTIC_CACHE",
    "COL_SEM_METRIC",
    "COL_SKILL",
    "DBA_COLLECTION_PREFIX",
    "CollectionSpec",
]

#: ★ 本项目 collection 统一前缀（虚拟机与其他项目共库，避免命名冲突）
DBA_COLLECTION_PREFIX = "dba_"

# ── 5 个 collection 的权威名字（唯一真相源，散落处一律引用这些常量）──────
COL_SEM_METRIC = f"{DBA_COLLECTION_PREFIX}sem_metric_vec"
COL_SKILL = f"{DBA_COLLECTION_PREFIX}skill_vec"
COL_SEMANTIC_CACHE = f"{DBA_COLLECTION_PREFIX}semantic_cache_vec"
COL_FINOPS_KB = f"{DBA_COLLECTION_PREFIX}finops_kb_vec"
COL_OBS_ANOMALY = f"{DBA_COLLECTION_PREFIX}obs_anomaly_vec"

#: ★ P1-1：跨存储统一哨兵 0（不是 -1）
BIZ_LINE_SENTINEL = 0


@dataclass(frozen=True)
class CollectionSpec:
    """一个 collection 的物理规格。"""

    name: str
    owner: str
    description: str
    metric_type: str = "COSINE"
    index_type: str = "HNSW"
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    #: 除向量与主键外的标量字段（名, 类型）——类型用 Milvus DataType 名字符串
    scalar_fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: 是否带 ``ttl_epoch``（缓存类 collection 需要）
    has_ttl: bool = False


COLLECTION_SPECS: tuple[CollectionSpec, ...] = (
    CollectionSpec(
        name=COL_SEM_METRIC,
        owner="capabilities.semantics",
        description="指标口径语义检索",
        scalar_fields=(("metric_code", "VARCHAR"), ("biz_line_id", "INT64")),
    ),
    CollectionSpec(
        name=COL_SKILL,
        owner="capabilities.skills",
        description="技能意图匹配",
        scalar_fields=(("skill_key", "VARCHAR"), ("biz_line_id", "INT64")),
    ),
    CollectionSpec(
        name=COL_SEMANTIC_CACHE,
        owner="capabilities.memory",
        description="语义缓存（检索须叠加 ttl_epoch > now）",
        scalar_fields=(
            ("query_hash", "VARCHAR"),
            ("biz_line_id", "INT64"),
            ("scope_hash", "VARCHAR"),
        ),
        has_ttl=True,
    ),
    CollectionSpec(
        name=COL_FINOPS_KB,
        owner="modules.finops",
        description="降本知识库检索",
        scalar_fields=(("doc_id", "VARCHAR"), ("biz_line_id", "INT64")),
    ),
    CollectionSpec(
        name=COL_OBS_ANOMALY,
        owner="modules.observability",
        description="历史异常相似召回",
        scalar_fields=(("alert_id", "INT64"), ("biz_line_id", "INT64")),
    ),
)
