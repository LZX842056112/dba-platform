"""Elasticsearch 存储适配层（L5）：检索索引 + ILM（§5.7）。

★ ``dynamic: strict``（3 个索引全部严格模式）：写入未声明字段会**报错**，
  因此必须有 DLQ——不能把「映射不匹配」当成静默丢弃（P1-9）。
"""

from __future__ import annotations

from .client import EsStorage, build_es
from .indices import INDEX_SPECS, IndexSpec
from .repo import EventIndexRepo

__all__ = ["INDEX_SPECS", "EsStorage", "EventIndexRepo", "IndexSpec", "build_es"]
