"""MinIO 客户端封装（minio SDK，懒加载 + 线程池桥接）。

★ minio SDK 是同步阻塞客户端，用 ``asyncio.to_thread`` 桥接到 async 契约。
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from dba_runtime.errors import StorageUnavailableError

from .buckets import BUCKETS

__all__ = ["MinioStorage", "build_minio"]

logger = logging.getLogger("dba.storage.minio")


def build_minio(endpoint: str, access_key: str, secret_key: str, *, secure: bool = False) -> Any:
    try:
        from minio import Minio  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "MinIO 需要安装 `dba[minio]`（minio）", detail={"component": "minio"}
        ) from exc
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


class MinioStorage:
    """MinIO 存储聚合：客户端 + bucket 初始化 + put/get/删除。"""

    def __init__(
        self, endpoint: str, access_key: str, secret_key: str, *, secure: bool = False
    ) -> None:
        self._client: Any = build_minio(endpoint, access_key, secret_key, secure=secure)

    @property
    def client(self) -> Any:
        return self._client

    async def ping(self) -> bool:
        try:
            await asyncio.to_thread(self._client.list_buckets)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def ensure_buckets(self) -> None:
        await asyncio.to_thread(self._ensure_buckets_sync)

    def _ensure_buckets_sync(self) -> None:
        for bucket in BUCKETS:
            if not self._client.bucket_exists(bucket.name):
                self._client.make_bucket(bucket.name)
        # 注意：公开读策略在此**不自动设置**（生产应显式 apply policy，见 scripts/bootstrap.sh）

    async def put(
        self, bucket: str, key: str, data: bytes, content_type: str, length: int | None = None
    ) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            bucket,
            key,
            io.BytesIO(data),
            length if length is not None else len(data),
            content_type=content_type,
        )
        return f"{bucket}/{key}"

    async def presigned_get(self, bucket: str, key: str, *, expires_s: int) -> str:
        from datetime import timedelta  # noqa: PLC0415

        url = await asyncio.to_thread(
            self._client.presigned_get_object,
            bucket,
            key,
            expires=timedelta(seconds=expires_s),
        )
        return str(url)

    async def get(self, bucket: str, key: str) -> bytes:
        def _read() -> bytes:
            response = self._client.get_object(bucket, key)
            try:
                return bytes(response.read())
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_read)

    async def delete(self, bucket: str, key: str) -> None:
        await asyncio.to_thread(self._client.remove_object, bucket, key)

    async def aclose(self) -> None:  # pragma: no cover - SDK 无显式关闭
        return None
