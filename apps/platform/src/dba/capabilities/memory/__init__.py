"""记忆能力（owner: capabilities.memory）。

对齐《设计文档 v2》§6.6、§5.4 ``semantic_cache_entry``、§5.3 ``dba_semantic_cache_vec``
与《实现要点清单》§6.6（scope_hash）、U16。
"""

from __future__ import annotations

from .cache import MemoryService, QueryCacheKey
from .scope import ScopeFingerprint, build_scope_hash, canonical_json

__all__ = [
    "MemoryService",
    "QueryCacheKey",
    "ScopeFingerprint",
    "build_scope_hash",
    "canonical_json",
]
