"""MinIO 对象存储适配层（L5）：5 个 bucket（§5.8）。"""

from __future__ import annotations

from .buckets import BUCKETS, Bucket
from .client import MinioStorage, build_minio
from .repo import ObjectStoreRepo

__all__ = ["BUCKETS", "Bucket", "MinioStorage", "ObjectStoreRepo", "build_minio"]
