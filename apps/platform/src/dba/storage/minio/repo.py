"""MinIO ``ObjectStoreRepo`` 实现（Protocol 见 ``storage/protocols.py``）。"""

from __future__ import annotations

from .client import MinioStorage

__all__ = ["ObjectStoreRepo"]


class ObjectStoreRepo:
    def __init__(self, storage: MinioStorage) -> None:
        self._storage = storage

    async def ensure_buckets(self) -> None:
        await self._storage.ensure_buckets()

    async def put(
        self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        return await self._storage.put(bucket, key, data, content_type)

    async def presigned_get(self, bucket: str, key: str, *, expires_s: int = 3600) -> str:
        return await self._storage.presigned_get(bucket, key, expires_s=expires_s)

    async def get(self, bucket: str, key: str) -> bytes:
        return await self._storage.get(bucket, key)

    async def delete(self, bucket: str, key: str) -> None:
        await self._storage.delete(bucket, key)
