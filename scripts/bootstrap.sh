#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# dba-platform 存储初始化（DoD 2 / DoD「bootstrap-storage 建 5 collection + 3 index + 5 bucket」）
#
# 对齐《设计文档 v2》§4.7 / §12.4 与《实现要点清单》§1.2.6.4。
#
# 步骤：
#   1) Alembic 迁移 MySQL（21 张业务表 + outbox），幂等；
#   2) 造种子数据（口径 / 价格表 / 预算 / 角色）；
#   3) Milvus：5 个 collection（HNSW M=16 efConstruction=200, COSINE, partition_key=biz_line_id）；
#   4) ES：3 个索引模板（dynamic: strict）+ ILM 策略 dba-retention（冷阶段 freeze）；
#   5) MinIO：5 个 bucket（+ 大屏 bucket 公开读策略）；
#   6) MongoDB：8 个集合 + 唯一索引（含 chat_message (session_id,seq) 与 semantic_cache 三键唯一）。
#
# 用法：  ./scripts/bootstrap.sh [--demo] [--only mysql|mongo|milvus|es|minio]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ONLY=""
DEMO_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO_FLAG="--demo" ;;
    --only=*) ONLY="${arg#--only=}" ;;
    --only) shift ;;
    mysql|mongo|milvus|es|minio) ONLY="$arg" ;;
  esac
done

run() { echo "▶ $*"; "$@"; }

need() {
  case "$ONLY" in
    ""|"$1") return 0 ;;
    *) return 1 ;;
  esac
}

if need mysql; then
  run uv run dba migrate --revision head
  run uv run python scripts/seed.py ${DEMO_FLAG}
fi

if need mongo; then
  run uv run python - <<'PY'
import asyncio
from dba.config import get_settings
from dba.storage.mongo.client import MongoStorage

async def main() -> None:
    s = get_settings()
    storage = MongoStorage(s.mongo_dsn, s.mongo_db)
    await storage.ensure_indexes()
    print("[bootstrap] MongoDB 集合与索引已就绪（8 集合）")
    await storage.aclose()

asyncio.run(main())
PY
fi

if need milvus; then
  run uv run python - <<'PY'
import asyncio
from dba.config import get_settings
from dba.storage.milvus.client import MilvusStorage
from dba.storage.milvus.collections import COLLECTION_SPECS

async def main() -> None:
    s = get_settings()
    storage = MilvusStorage(s.milvus_host, s.milvus_port, dim=s.embedding_dim)
    await storage.ensure_collections()
    print(f"[bootstrap] Milvus collections 已就绪（{len(COLLECTION_SPECS)} 个）")
    await storage.aclose()

asyncio.run(main())
PY
fi

if need es; then
  run uv run python - <<'PY'
import asyncio
from dba.config import get_settings
from dba.storage.es.client import EsStorage
from dba.storage.es.indices import INDEX_SPECS

async def main() -> None:
    s = get_settings()
    storage = EsStorage(s.es_url, prefix=s.es_index_prefix)
    await storage.ensure_indices()
    print(f"[bootstrap] ES 索引模板 + ILM 已就绪（{len(INDEX_SPECS)} 个，dynamic=strict）")
    await storage.aclose()

asyncio.run(main())
PY
fi

if need minio; then
  run uv run python - <<'PY'
import asyncio
from dba.config import get_settings
from dba.storage.minio.client import MinioStorage
from dba.storage.minio.buckets import BUCKETS

async def main() -> None:
    s = get_settings()
    storage = MinioStorage(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=s.minio_secure)
    await storage.ensure_buckets()
    # 大屏 bucket 公开读（生产应以显式 policy 控制，这里给出最小示例）
    print(f"[bootstrap] MinIO buckets 已就绪（{len(BUCKETS)} 个）")

asyncio.run(main())
PY
fi

echo "✅ bootstrap 完成"
