"""语义层能力（owner: capabilities.semantics）。

对齐《设计文档 v2》§5.2.2 与《实现要点清单》§5.3。含权限子域（组织/角色/行级规则）。
"""

from __future__ import annotations

from .service import MetricDef, SemanticService, build_scope_from_rules

__all__ = ["MetricDef", "SemanticService", "build_scope_from_rules"]
