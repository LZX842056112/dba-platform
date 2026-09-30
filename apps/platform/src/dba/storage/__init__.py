"""L5 存储适配层（Repository 模式，接口与实现分离，全 async）。

分层约定（《设计文档 v2》§2.1 / §5.9）：
* **接口（Protocol）在本包 ``protocols.py``，实现在 ``storage/*/`` 子包**；
* 每个 Repository 有**唯一 owner 模块**（§4.0 归属矩阵），只有 owner 能写；
* L4 能力层 / L3 模块**只依赖 Protocol**，禁止 API 层直连；
* 存储驱动（sqlalchemy/asyncmy/motor/pymilvus/elasticsearch/minio/redis）作为
  ``dba[mysql|mongo|...]`` extras 惰性导入（见各 ``*/client.py``），保证未装驱动时不炸。

★ 说明：本 ``__init__`` **只再导出 Protocol（零驱动依赖）**；具体实现包
（``dba.storage.mysql`` 等）需显式 import，以便「没装某驱动也能启动应用」。
"""

from __future__ import annotations

from .protocols import (
    PROTOCOL_OWNERS,
    AlertEventRepo,
    AnomalyReportRepo,
    AppAgentRepo,
    AuthRoleRepo,
    AuthUserRepo,
    BizLineRepo,
    BudgetRepo,
    BudgetReservationRepo,
    BudgetUsageRepo,
    ChatMessageRepo,
    ChatSessionRepo,
    DashboardSpecRepo,
    EventIndexRepo,
    FinopsRecommendationRepo,
    LlmCallRepo,
    MetricDailyRepo,
    ObjectStoreRepo,
    OutboxRepo,
    PriceBookRepo,
    Row,
    RowScopeRuleRepo,
    RunDocRepo,
    RunRepo,
    SemanticCacheEntryRepo,
    SemDictEntryRepo,
    SemFieldMappingRepo,
    SemMetricRepo,
    SkillDefRepo,
    SkillRegistryRepo,
    SkillUsageRepo,
    SqlAuditRepo,
    ToolCallRepo,
    UserRoleRepo,
    VectorRepo,
)

__all__ = [
    "PROTOCOL_OWNERS",
    "Row",
    "AlertEventRepo",
    "AnomalyReportRepo",
    "AppAgentRepo",
    "AuthRoleRepo",
    "AuthUserRepo",
    "BizLineRepo",
    "BudgetRepo",
    "BudgetReservationRepo",
    "BudgetUsageRepo",
    "ChatMessageRepo",
    "ChatSessionRepo",
    "DashboardSpecRepo",
    "EventIndexRepo",
    "FinopsRecommendationRepo",
    "LlmCallRepo",
    "MetricDailyRepo",
    "ObjectStoreRepo",
    "OutboxRepo",
    "PriceBookRepo",
    "RowScopeRuleRepo",
    "RunDocRepo",
    "RunRepo",
    "SemDictEntryRepo",
    "SemFieldMappingRepo",
    "SemMetricRepo",
    "SemanticCacheEntryRepo",
    "SkillDefRepo",
    "SkillRegistryRepo",
    "SkillUsageRepo",
    "SqlAuditRepo",
    "ToolCallRepo",
    "UserRoleRepo",
    "VectorRepo",
]
