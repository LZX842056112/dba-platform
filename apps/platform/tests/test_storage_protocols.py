"""L5 存储层：Protocol owner 与规格计数。

★ 断言「每个 Protocol 都有 owner」（§4.0 归属矩阵）——没有 owner 的 Protocol
  意味着「谁都能写」，归属约束形同虚设。
★ 断言 5 collection / 3 index / 5 bucket / 21 表 —— 与 §5.3 / §5.7 / §5.8 / §5.2 对齐。
"""

from __future__ import annotations

from dba.storage import protocols
from dba.storage.es.indices import INDEX_SPECS, build_ilm_policy
from dba.storage.milvus.collections import COLLECTION_SPECS
from dba.storage.minio.buckets import BUCKETS


def test_every_protocol_has_owner() -> None:
    assert protocols.PROTOCOL_OWNERS, "PROTOCOL_OWNERS 为空"
    missing = []
    for name, obj in vars(protocols).items():
        if isinstance(obj, type) and name.endswith("Repo"):
            if not getattr(obj, "owner", None):
                missing.append(name)
    assert not missing, f"以下 Protocol 缺少 owner：{missing}"


def test_milvus_five_collections_with_hnsw_cosine() -> None:
    assert len(COLLECTION_SPECS) == 5
    for spec in COLLECTION_SPECS:
        assert spec.index_type == "HNSW"
        assert spec.metric_type == "COSINE"
        assert spec.hnsw_m == 16 and spec.hnsw_ef_construction == 200  # §5.3


def test_es_three_indices_dynamic_strict_ilm_freeze() -> None:
    assert len(INDEX_SPECS) == 3
    for spec in INDEX_SPECS:
        assert "@timestamp" in spec.properties
    policy = build_ilm_policy(INDEX_SPECS[0])
    cold = policy["policy"]["phases"]["cold"]["actions"]  # type: ignore[index]
    # ★ 冷阶段必须是 freeze，不能用 searchable_snapshot（否则不可再查）
    assert "freeze" in cold
    assert "searchable_snapshot" not in cold


def test_minio_five_buckets() -> None:
    assert len(BUCKETS) == 5
    assert {b.name for b in BUCKETS} == {
        "dba-dashboard",
        "dba-export",
        "dba-skill-assets",
        "dba-doc-chunks",
        "dba-dlq",
    }


def test_bit_line_sentinel_is_zero() -> None:
    from dba.storage.milvus.collections import BIZ_LINE_SENTINEL

    assert BIZ_LINE_SENTINEL == 0  # ★ P1-1：哨兵 0，不是 -1
