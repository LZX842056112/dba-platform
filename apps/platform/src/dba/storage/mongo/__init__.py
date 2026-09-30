"""MongoDB 存储适配层（L5）：文档型存储（会话 / span 树 / 技能定义 / 归因）。

对齐《设计文档 v2》§5.4 与《实现要点清单》§5.5。共 8 个集合。
"""

from __future__ import annotations

from .client import MongoStorage, build_mongo
from .repo import (
    AnomalyReportRepo,
    ChatMessageRepo,
    ChatSessionRepo,
    DashboardSpecRepo,
    FinopsRecommendationRepo,
    MongoRepositories,
    RunDocRepo,
    SemanticCacheEntryRepo,
    SkillDefRepo,
)

__all__ = [
    "AnomalyReportRepo",
    "ChatMessageRepo",
    "ChatSessionRepo",
    "DashboardSpecRepo",
    "FinopsRecommendationRepo",
    "MongoRepositories",
    "MongoStorage",
    "RunDocRepo",
    "SemanticCacheEntryRepo",
    "SkillDefRepo",
    "build_mongo",
]
