"""向量化客户端（HTTP + 离线回退 + LRU 缓存）。

★ 为什么必须有离线 embedder（照抄 v1 会怎样错）
--------------------------------------------
v1 在单测/本地无向量服务时会让 ``embed`` 直接抛错，导致依赖 embedding 的
语义检索/技能匹配**整条路径无法测试**，只能靠 mock 掉整层——掩盖真实逻辑。
v2 提供 ``HashEmbedder``：确定性、零依赖，让「相同文本 → 相同向量」这一契约可被真实单测。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import struct
import urllib.request
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from dba_runtime.errors import StorageUnavailableError

__all__ = ["EmbedderProto", "EmbeddingCache", "HashEmbedder", "HttpEmbedder"]

logger = logging.getLogger("dba.capabilities.embedding")


@runtime_checkable
class EmbedderProto(Protocol):
    """向量化接口（同步 ``embed_one`` + 异步 ``embed``）。"""

    @property
    def dim(self) -> int: ...

    def embed_one(self, text: str) -> list[float]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingCache:
    """按文本哈希的 LRU 缓存（避免重复计费/重复网络请求）。"""

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = capacity
        self._store: OrderedDict[str, list[float]] = OrderedDict()

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> list[float] | None:
        k = self.key(text)
        if k not in self._store:
            return None
        self._store.move_to_end(k)
        return self._store[k]

    def put(self, text: str, vector: list[float]) -> None:
        k = self.key(text)
        self._store[k] = vector
        self._store.move_to_end(k)
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)


class HashEmbedder:
    """确定性伪向量（离线/单测）。相同文本 → 相同向量，且已做 L2 归一化。"""

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        tokens = text.strip().lower().split() or [text]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            # 每个 token 影响 8 个维度（哈希投影），保证相似文本向量相近
            for i in range(0, 32, 4):
                idx = int.from_bytes(digest[i : i + 4], "big") % self._dim
                sign = 1.0 if digest[i] % 2 == 0 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class HttpEmbedder:
    """外部向量化服务客户端。

    ``url`` 为 base URL（不含路径），``path`` 为嵌入端点：本地 bge-m3 服务用 ``/embed``
    （返回 ``{"vectors":[...]}/{ "data":[...]}``）；OpenAI 兼容服务用 ``/v1/embeddings``
    （返回 ``{"data":[{"embedding":[...]}]}``）。响应两种格式均已兼容。
    """

    def __init__(
        self,
        url: str,
        *,
        path: str = "/embed",
        dim: int = 1024,
        model: str = "bge-large-zh-v1.5",
        timeout_s: float = 10.0,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._path = path if path.startswith("/") else f"/{path}"
        self._dim = dim
        self._model = model
        self._timeout_s = timeout_s
        self._cache = cache or EmbeddingCache()

    @property
    def dim(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        """同步取单条向量（命中缓存则直接返回）。未命中且无缓存时 **抛错**（调用方应走异步）。"""
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        raise StorageUnavailableError(
            "HttpEmbedder.embed_one 未命中缓存；请先 await embed([...]) 预热",
            detail={"component": "embedding"},
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        pending: list[str] = []
        result: list[list[float] | None] = []
        for text in texts:
            cached = self._cache.get(text)
            result.append(cached)
            if cached is None and text not in pending:
                pending.append(text)
        if pending:
            vectors = await asyncio.to_thread(self._post, pending)
            by_text = dict(zip(pending, vectors, strict=True))
            for i, text in enumerate(texts):
                if result[i] is None:
                    vector = by_text[text]
                    self._cache.put(text, vector)
                    result[i] = vector
        return [v if v is not None else [0.0] * self._dim for v in result]

    def _post(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self._model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - 内部可信地址
            f"{self._url}{self._path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as resp:  # noqa: S310
                body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError(
                f"向量化服务不可用：{exc}", detail={"component": "embedding"}
            ) from exc
        vectors = body.get("vectors") or body.get("data") or []
        out: list[list[float]] = []
        for item in vectors:
            if isinstance(item, dict):  # OpenAI 兼容格式
                item = item.get("embedding", [])
            out.append([float(x) for x in item])
        if len(out) != len(texts):
            raise StorageUnavailableError(
                f"向量化返回条数不匹配：期望 {len(texts)} 得到 {len(out)}",
                detail={"component": "embedding"},
            )
        return out


_ = struct  # 保留以示意可扩展为二进制打包传输
