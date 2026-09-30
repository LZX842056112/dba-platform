"""本地 bge-m3 向量化服务（FastAPI）。

契约（与 ``dba.capabilities.embedding.client.HttpEmbedder`` 对齐）：
  * ``POST /embed``：
      请求 ``{"model": "bge-m3", "input": ["文本", ...]}``（``input`` 兼容单字符串 / ``texts``）；
      响应 ``{"vectors": [[...1024 维...], ...], "data": [{...OpenAI 兼容...}], "dim": 1024}``
  * ``GET /health``：``{"status": "ok", "model": ..., "device": ..., "dim": ...}``

模型来源：本地 ModelScope 缓存目录（``MODEL_NAME`` 指向本地路径，不再走 HF 下载）。
模型懒加载（首次请求时载入），进程内单例。

运行（复用 python311 conda 环境，其已含 torch/sentence-transformers/fastapi/uvicorn）：:

    MODEL_NAME="D:/cache/modelscope/hub/models/BAAI/bge-m3" \\
      "C:/MySoftware/Anaconda3/envs/python311/python.exe" deploy/embedding_server.py
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

# ★ 默认指向本地 ModelScope 缓存的 bge-m3；可用环境变量覆盖
MODEL_NAME = os.environ.get("MODEL_NAME", "D:/cache/modelscope/hub/models/BAAI/bge-m3")
DEVICE = os.environ.get("DEVICE", "cpu")
DEFAULT_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
PORT = int(os.environ.get("PORT", "8100"))

app = FastAPI(title="dba-embedding-local", version="1.0.0")

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """懒加载模型（首次调用时载入，进程内单例）。"""
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    return _model


class EmbedRequest(BaseModel):
    """``/embed`` 请求体（宽容解析：``input`` / ``texts`` 二选一）。"""

    model: str | None = None
    input: str | list[str] | None = None
    texts: list[str] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    """健康探针（不触发模型加载，便于容器启动期探活）。"""
    return {"status": "ok", "model": MODEL_NAME, "device": DEVICE, "dim": DEFAULT_DIM}


@app.post("/embed")
def embed(req: EmbedRequest) -> dict[str, Any]:
    """文本 → 向量（L2 归一化，配合 Milvus COSINE 度量）。"""
    raw: str | list[str] | None = req.input if req.input is not None else req.texts
    texts: list[str] = [raw] if isinstance(raw, str) else list(raw or [])
    if not texts:
        return {"vectors": [], "data": [], "dim": DEFAULT_DIM}
    vectors: list[list[float]] = get_model().encode(texts, normalize_embeddings=True).tolist()
    return {
        "object": "list",
        "model": "bge-m3",
        "dim": len(vectors[0]) if vectors else DEFAULT_DIM,
        "vectors": vectors,
        "data": [
            {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
