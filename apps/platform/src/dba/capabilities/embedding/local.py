"""本地 in-process 向量化（加载本地 bge-m3 模型，无需独立 HTTP 服务）。

对比 ``HttpEmbedder``（走外部服务）：本实现把模型加载进**当前进程**，省掉一个常驻
服务进程，代价是 torch 与模型权重（~4GB 内存）进入 app 进程。

★ 惰性导入：``sentence_transformers`` 只在首次真正编码时才 import —— 这样当
``embedding_backend="http"``（或未安装 torch）时，app 仍可正常 import 与单测。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dba_runtime.errors import StorageUnavailableError

from .client import EmbeddingCache

__all__ = ["LocalEmbedder"]

logger = logging.getLogger("dba.capabilities.embedding.local")


class LocalEmbedder:
    """本地模型向量化（``SentenceTransformer`` 懒加载，进程内单例）。"""

    def __init__(
        self,
        model_path: str,
        *,
        dim: int = 1024,
        device: str = "cpu",
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._path = model_path
        self._dim = dim
        self._device = device
        self._cache = cache or EmbeddingCache()
        self._model: Any | None = None  # 懒加载

    @property
    def dim(self) -> int:
        return self._dim

    def _get_model(self) -> Any:
        """懒加载模型（首次调用才 import + 载入，进程内单例）。"""
        if self._model is None:
            # ★ 惰性导入：无 torch 环境下不 import 此模块
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415

            logger.info("加载本地 embedding 模型：%s（device=%s）", self._path, self._device)
            self._model = SentenceTransformer(self._path, device=self._device)
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """同步编码（CPU/GPU 密集，仅供 to_thread 调用）。"""
        model = self._get_model()
        return model.encode(texts, normalize_embeddings=True).tolist()  # type: ignore[no-any-return]

    def embed_one(self, text: str) -> list[float]:
        """同步取单条向量（命中缓存直接返回；未命中则即时编码）。"""
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        vector = self._encode([text])[0]
        self._cache.put(text, vector)
        return vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码：先查缓存，未命中的批量丢线程池编码，回填缓存。"""
        pending: list[str] = []
        result: list[list[float] | None] = []
        for text in texts:
            cached = self._cache.get(text)
            result.append(cached)
            if cached is None and text not in pending:
                pending.append(text)
        if pending:
            try:
                vectors = await asyncio.to_thread(self._encode, pending)
            except Exception as exc:  # noqa: BLE001
                raise StorageUnavailableError(
                    f"本地向量化失败：{exc}", detail={"component": "embedding", "model": self._path}
                ) from exc
            by_text = dict(zip(pending, vectors, strict=True))
            for i, text in enumerate(texts):
                if result[i] is None:
                    vector = by_text[text]
                    self._cache.put(text, vector)
                    result[i] = vector
        return [v if v is not None else [0.0] * self._dim for v in result]
