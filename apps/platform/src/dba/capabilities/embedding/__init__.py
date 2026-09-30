"""向量化能力（owner: capabilities.embedding）。

对齐《设计文档 v2》§3.1 / §5.8 与《实现要点清单》§5.8。

★ 提供三种 embedder：
  * ``HttpEmbedder``：调用外部向量化服务（``DBA_EMBEDDING_URL``），**带 LRU 缓存**；
  * ``HashEmbedder``：确定性伪向量（**离线/单测**用；不依赖网络，保证同文本同向量）；
  * ``EmbedderProto``：接口契约（便于替换与 mock）。
"""

from __future__ import annotations

from .client import EmbedderProto, EmbeddingCache, HashEmbedder, HttpEmbedder

__all__ = ["EmbedderProto", "EmbeddingCache", "HashEmbedder", "HttpEmbedder"]
