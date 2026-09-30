"""L5 存储层 Protocol 定义（**接口契约**）。

对齐《设计文档 v2》§5.9 归属矩阵 与《实现要点清单》§4.0 / §5（L5）。

★ 铁律：**每个 Protocol 必须标注 ``owner``**（唯一写入者的模块名）。
   ``owner`` 用 ``ClassVar[str]`` 声明，既可读也可被单测/静态检查消费；
   非 owner 模块 import 具体实现由 Ruff ``banned-api`` 拦截（见根 ``pyproject.toml``）。

约定：
* 行数据统一用 ``Row = dict[str, Any]``（列名 → 值），避免把 ORM 对象泄漏到 L4/L3；
* 所有 I/O 方法均为 ``async``；
* 金额字段一律 micro_usd 整数（§6.1）；时间为 UTC（§6.2）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar, Protocol, runtime_checkable

from dba_runtime.tools.sql_tool import ExecMeta

__all__ = [
    "Row",
    "PROTOCOL_OWNERS",
    # MySQL · 权限/语义（owner=capabilities.semantics）
    "BizLineRepo",
    "AuthUserRepo",
    "AuthRoleRepo",
    "UserRoleRepo",
    "RowScopeRuleRepo",
    "SemMetricRepo",
    "SemFieldMappingRepo",
    "SemDictEntryRepo",
    # MySQL · 埋点/成本（owner=capabilities.telemetry / capabilities.cost）
    "RunRepo",
    "LlmCallRepo",
    "ToolCallRepo",
    "BudgetReservationRepo",
    "PriceBookRepo",
    "OutboxRepo",
    # MySQL · 预算（owner=capabilities.budget）
    "BudgetRepo",
    "BudgetUsageRepo",
    # MySQL · 技能（owner=capabilities.skills）
    "SkillRegistryRepo",
    "SkillUsageRepo",
    # MySQL · 观测（owner=modules.observability）
    "AppAgentRepo",
    "AlertEventRepo",
    "MetricDailyRepo",
    # MySQL · ChatBI（owner=modules.chatbi）
    "SqlAuditRepo",
    # Mongo
    "ChatSessionRepo",
    "ChatMessageRepo",
    "DashboardSpecRepo",
    "RunDocRepo",
    "SkillDefRepo",
    "AnomalyReportRepo",
    "FinopsRecommendationRepo",
    "SemanticCacheEntryRepo",
    # 派生存储
    "VectorRepo",
    "EventIndexRepo",
    "ObjectStoreRepo",
]

#: 统一的行表示（列名 → 值）
Row = dict[str, Any]


class _Owned(Protocol):
    """所有 Repository 的公共约定：声明唯一写入者。"""

    owner: ClassVar[str]


# ═════════════════════════════════════════════════════════════════════
# MySQL · 组织与权限（owner: capabilities.semantics —— 权限子域）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class BizLineRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``biz_line``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def get(self, biz_line_id: int) -> Row | None: ...
    async def list_active(self) -> list[Row]: ...


@runtime_checkable
class AuthUserRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``auth_user``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def by_username(self, username: str) -> Row | None: ...
    async def get(self, user_id: int) -> Row | None: ...


@runtime_checkable
class AuthRoleRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``auth_role``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def roles_of_user(self, user_id: int) -> list[Row]: ...
    async def role_ids_of_user(self, user_id: int) -> list[int]: ...


@runtime_checkable
class UserRoleRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``auth_user_role``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def grant(self, user_id: int, role_id: int) -> None: ...


@runtime_checkable
class RowScopeRuleRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``row_scope_rule``）。

    ★ P0-10：``rule_version()`` 返回该角色集 ``row_scope_rule`` 的**最大 ``updated_at``**
    （ISO8601 字符串），参与 ``scope_hash``；否则改了规则 hash 不变 → 继续命中旧权限缓存。
    """

    owner: ClassVar[str] = "capabilities.semantics"

    async def rules_for_roles(self, role_ids: list[int]) -> list[Row]: ...
    async def rule_version(self, role_ids: list[int]) -> str | None: ...


@runtime_checkable
class SemMetricRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``sem_metric``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def search(self, keyword: str, biz_line_id: int | None, limit: int = 8) -> list[Row]: ...
    async def by_codes(self, codes: list[str], biz_line_id: int | None) -> list[Row]: ...


@runtime_checkable
class SemFieldMappingRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``sem_field_mapping``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def by_logical(self, logical_field: str) -> list[Row]: ...
    async def all_mappings(self) -> list[Row]: ...


@runtime_checkable
class SemDictEntryRepo(_Owned, Protocol):
    """owner: ``capabilities.semantics``（表 ``sem_dict_entry``）。"""

    owner: ClassVar[str] = "capabilities.semantics"

    async def search(self, term: str, biz_line_id: int | None) -> list[Row]: ...


# ═════════════════════════════════════════════════════════════════════
# MySQL · Run 埋点与成本明细（owner: capabilities.telemetry / capabilities.cost）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class RunRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（表 ``run``，按月分区）。

    ★ 主键 ``(trace_id, started_at)``；只按 ``trace_id`` 查无法分区裁剪，
    因此读取必须带时间窗（§6.10）。
    """

    owner: ClassVar[str] = "capabilities.telemetry"

    async def insert(self, row: Row) -> None: ...
    async def finish(self, trace_id: str, *, started_at: datetime, row: Row) -> int: ...
    async def get(self, trace_id: str, *, since: datetime, until: datetime) -> Row | None: ...
    async def list_runs(self, flt: Row, *, limit: int = 50) -> list[Row]: ...


@runtime_checkable
class LlmCallRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（表 ``llm_call``，高写入 → Core 批量）。"""

    owner: ClassVar[str] = "capabilities.telemetry"

    async def bulk_insert(self, rows: list[Row]) -> int: ...


@runtime_checkable
class ToolCallRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（表 ``tool_call``）。"""

    owner: ClassVar[str] = "capabilities.telemetry"

    async def bulk_insert(self, rows: list[Row]) -> int: ...


@runtime_checkable
class BudgetReservationRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（表 ``budget_reservation``，权威账本）。

    ★ P0-4：预留在 **MySQL 有落点**，``settle``/``release`` **必须幂等**
    （状态机 ``RESERVED → SETTLED|RELEASED|EXPIRED``，非法迁移不改数）。
    """

    owner: ClassVar[str] = "capabilities.telemetry"

    async def insert_if_absent(self, row: Row) -> bool:
        """插入预留；已存在（同 ``reservation_id``）返回 ``False``，不覆盖。"""
        ...

    async def settle(self, reservation_id: str, actual_micro_usd: int) -> Row:
        """结算；返回 ``{status, estimated_micro_usd}``。

        ``status ∈ settled | already_settled | reservation_missing``。
        """
        ...

    async def release(self, reservation_id: str) -> Row:
        """释放；返回 ``{status, estimated_micro_usd}``。

        ``status ∈ released | already_released | reservation_missing``。
        """
        ...

    async def sum_reserved(self, budget_id: int, period_start: date) -> int: ...
    async def expire_stale(self, now: datetime) -> list[Row]: ...


@runtime_checkable
class PriceBookRepo(_Owned, Protocol):
    """owner: ``capabilities.cost``（表 ``price_book``）。"""

    owner: ClassVar[str] = "capabilities.cost"

    async def lookup(self, provider: str, model: str, at: datetime) -> Row | None: ...
    async def upsert(self, row: Row) -> None: ...
    async def list_active(self) -> list[Row]: ...


@runtime_checkable
class OutboxRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（表 ``outbox``）。

    ★ P1-6：**禁止内存队列**。埋点与 outbox 记录同事务落库，崩溃后未投递记录仍可补投。
    """

    owner: ClassVar[str] = "capabilities.telemetry"

    async def enqueue(self, rows: list[Row]) -> None: ...
    async def claim(self, *, limit: int = 200) -> list[Row]: ...
    async def mark_done(self, ids: list[int]) -> int: ...
    async def mark_retry(self, outbox_id: int, error: str) -> None:
        """★ 重试：``attempts += 1``、记录错误，**状态回到 pending**（下轮继续投递）。"""
        ...

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """终态失败：``attempts += 1``、置 ``failed``（调用方负责落 DLQ）。"""
        ...

    async def pending_count(self) -> int: ...


# ═════════════════════════════════════════════════════════════════════
# MySQL · 预算（owner: capabilities.budget）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class BudgetRepo(_Owned, Protocol):
    """owner: ``capabilities.budget``（表 ``budget``）。

    ★ U15：取出的是 ``enabled=1 AND version=max(version)`` 的那一行。
    """

    owner: ClassVar[str] = "capabilities.budget"

    async def effective(self, scope_type: str, scope_id: str, period: str) -> Row | None: ...
    async def by_id(self, budget_id: int) -> Row | None: ...
    async def upsert(self, row: Row) -> int: ...


@runtime_checkable
class BudgetUsageRepo(_Owned, Protocol):
    """owner: ``capabilities.budget``（表 ``budget_usage``，MySQL 侧权威账本）。"""

    owner: ClassVar[str] = "capabilities.budget"

    async def get_or_create(self, budget_id: int, period_start: date) -> Row: ...
    async def apply_reserve(self, budget_id: int, period_start: date, amount: int) -> Row: ...
    async def reserve_atomic(
        self, budget_id: int, period_start: date, *, amount: int, hard_limit: int
    ) -> Row:
        """★ P1-9 兜底：**行锁下**原子「判断上限 + 预留」。

        Redis 不可用时（fail-open 策略）改走此路径。返回
        ``{granted: bool, used: int, consumed: int, reserved: int}``；
        ``granted=False`` 时**不改数**（预留未生效）。
        """
        ...

    async def apply_settle(
        self, budget_id: int, period_start: date, *, released: int, actual: int
    ) -> Row: ...
    async def apply_release(self, budget_id: int, period_start: date, amount: int) -> Row: ...
    async def get(self, budget_id: int, period_start: date) -> Row | None: ...
    async def set_breaker(self, budget_id: int, period_start: date, state: str) -> None: ...


# ═════════════════════════════════════════════════════════════════════
# MySQL · 技能（owner: capabilities.skills）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class SkillRegistryRepo(_Owned, Protocol):
    """owner: ``capabilities.skills``（表 ``skill_registry``）。"""

    owner: ClassVar[str] = "capabilities.skills"

    async def by_key(self, skill_key: str, version: int | None = None) -> Row | None: ...
    async def match_intent(self, intent: str, biz_line_id: int | None) -> list[Row]: ...
    async def upsert(self, row: Row) -> int: ...
    async def stats(self, biz_line_id: int | None) -> Row: ...


@runtime_checkable
class SkillUsageRepo(_Owned, Protocol):
    """owner: ``capabilities.skills``（表 ``skill_usage``）。

    ★ P1-2：``UNIQUE(skill_id, trace_id)`` + upsert —— 自愈重试会重复经过同一技能，
    v1 会因此**灌水复用率**。
    """

    owner: ClassVar[str] = "capabilities.skills"

    async def record(
        self,
        *,
        skill_id: int,
        trace_id: str,
        biz_line_id: int | None,
        is_reuse: bool,
        tokens_saved_est: int = 0,
        baseline_cost_micro_usd: int = 0,
    ) -> None: ...


# ═════════════════════════════════════════════════════════════════════
# MySQL · 观测（owner: modules.observability）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class AppAgentRepo(_Owned, Protocol):
    """owner: ``modules.observability``（表 ``app_agent``）。"""

    owner: ClassVar[str] = "modules.observability"

    async def list_agents(self, flt: Row) -> list[Row]: ...
    async def upsert(self, row: Row) -> None: ...
    async def heartbeat(self, agent_uid: str, at: datetime) -> None: ...


@runtime_checkable
class AlertEventRepo(_Owned, Protocol):
    """owner: ``modules.observability``（表 ``alert_event``）。"""

    owner: ClassVar[str] = "modules.observability"

    async def insert(self, row: Row) -> int: ...
    async def list_events(self, flt: Row) -> list[Row]: ...
    async def ack(self, alert_id: int, user_id: int) -> int: ...


@runtime_checkable
class MetricDailyRepo(_Owned, Protocol):
    """owner: ``modules.observability``（表 ``metric_daily``，按月分区；worker rollup 写）。

    ★ P1-1：``biz_line_id`` 哨兵为 **0**（不是 -1），与 Milvus 分区键哨兵保持一致（§6.8）。
    """

    owner: ClassVar[str] = "modules.observability"

    async def upsert_many(self, rows: list[Row]) -> int: ...
    async def timeseries(self, flt: Row) -> list[Row]: ...


# ═════════════════════════════════════════════════════════════════════
# MySQL · ChatBI（owner: modules.chatbi）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class SqlAuditRepo(_Owned, Protocol):
    """owner: ``modules.chatbi``（表 ``sql_audit``，仅 ``QueryExecutor`` / 护栏链写）。

    ★ 三个写入口对应《设计方案 v2》§6.3.1 / §6.3.4 的调用点：
    ``write(...)``（护栏链 deny/rewrite、执行审计）、``mark_truncated(...)``（结果被截断）。
    ``insert`` / ``query`` 为底层原语，供审计查询 API 使用。
    """

    owner: ClassVar[str] = "modules.chatbi"

    async def insert(self, row: Row) -> int: ...
    async def query(self, flt: Row) -> list[Row]: ...

    async def write(
        self,
        *,
        trace_id: str,
        sql_text: str,
        decision: str,
        rewritten_sql: str | None = None,
        guard_stage: str | None = None,
        deny_reason: str | None = None,
        code: str | None = None,
        scope_injected: bool = False,
        scope_hash: str | None = None,
        rows_returned: int | None = None,
        exec_ms: int | None = None,
        user_id: int | None = None,
        biz_line_id: int | None = None,
    ) -> int:
        """写一条 SQL 审计（``decision ∈ allow|rewrite|deny|retry``）；返回审计行 id。

        ``sql_fingerprint = sha256(sql_text)[:64]``；``code``（符号错误码）会被折进
        ``deny_reason``（表无独立列），便于事后按错误码检索。
        """
        ...

    async def mark_truncated(self, trace_id: str, rows_returned: int, max_rows: int) -> int:
        """标记「最近一条该 trace 的审计被截断」；返回受影响行数。"""
        ...


# ═════════════════════════════════════════════════════════════════════
# MongoDB（8 集合）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class ChatSessionRepo(_Owned, Protocol):
    """owner: ``modules.chatbi``（集合 ``chat_session``）。"""

    owner: ClassVar[str] = "modules.chatbi"

    async def create(self, doc: Row) -> Row: ...
    async def get(self, session_id: str) -> Row | None: ...
    async def list_by_user(self, user_id: int, *, limit: int = 20) -> list[Row]: ...
    async def next_message_seq(self, session_id: str) -> int:
        """★ U16：``findOneAndUpdate($inc message_count)`` 原子分配 ``seq``（不撞唯一键）。"""
        ...


@runtime_checkable
class ChatMessageRepo(_Owned, Protocol):
    """owner: ``modules.chatbi``（集合 ``chat_message``）。"""

    owner: ClassVar[str] = "modules.chatbi"

    async def append(self, doc: Row) -> Row: ...
    async def list_by_session(self, session_id: str, *, limit: int = 50) -> list[Row]: ...


@runtime_checkable
class DashboardSpecRepo(_Owned, Protocol):
    """owner: ``modules.chatbi``（集合 ``dashboard_spec``）。"""

    owner: ClassVar[str] = "modules.chatbi"

    async def save_new_version(self, doc: Row) -> int: ...
    async def latest(self, dashboard_id: str) -> Row | None: ...
    async def versions(self, dashboard_id: str) -> list[Row]: ...
    async def mark_published(self, dashboard_id: str, url: str) -> None: ...


@runtime_checkable
class RunDocRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（集合 ``run_doc``，span 树）。"""

    owner: ClassVar[str] = "capabilities.telemetry"

    async def upsert(self, doc: Row) -> None: ...
    async def get(self, trace_id: str) -> Row | None: ...


@runtime_checkable
class SkillDefRepo(_Owned, Protocol):
    """owner: ``capabilities.skills``（集合 ``skill_def``）。"""

    owner: ClassVar[str] = "capabilities.skills"

    async def latest(self, skill_key: str) -> Row | None: ...
    async def save_version(self, doc: Row) -> None: ...


@runtime_checkable
class AnomalyReportRepo(_Owned, Protocol):
    """owner: ``modules.observability``（集合 ``anomaly_report``）。

    ★ U14：归因的**权威源**在此（MySQL ``alert_event.attribution_json`` 只存展示摘要）。
    """

    owner: ClassVar[str] = "modules.observability"

    async def save(self, doc: Row) -> None: ...
    async def get_by_alert(self, alert_id: int) -> Row | None: ...


@runtime_checkable
class FinopsRecommendationRepo(_Owned, Protocol):
    """owner: ``modules.finops``（集合 ``finops_recommendation``）。"""

    owner: ClassVar[str] = "modules.finops"

    async def save(self, doc: Row) -> None: ...
    async def list(self, flt: Row) -> list[Row]: ...
    async def set_status(self, reco_id: str, status: str) -> None: ...


@runtime_checkable
class SemanticCacheEntryRepo(_Owned, Protocol):
    """owner: ``capabilities.memory``（集合 ``semantic_cache_entry``）。

    ★ 唯一键 ``(query_hash, biz_line_id, scope_hash)``；命中必须回本集合校验 ``scope_hash``。
    """

    owner: ClassVar[str] = "capabilities.memory"

    async def get(self, query_hash: str, biz_line_id: int, scope_hash: str) -> Row | None: ...
    async def upsert(self, doc: Row) -> None: ...
    async def delete_expired(self, now: datetime) -> int: ...


# ═════════════════════════════════════════════════════════════════════
# 派生存储（Milvus / ES / MinIO）
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class VectorRepo(_Owned, Protocol):
    """owner: 与各自权威源一致（``capabilities.memory`` / ``skills`` / ``semantics``）。

    ★ 5 个 collection 只经 owner 的检索方法，**禁止模块直接 ``client.search``**（§5.9）。
    ★ ``dba_semantic_cache_vec`` 检索**必须叠加 ``ttl_epoch > now``**（否则命中已过期缓存）。
    """

    owner: ClassVar[str] = "capabilities.memory"

    async def ensure_collections(self) -> None: ...
    async def upsert(self, collection: str, rows: list[Row]) -> int: ...
    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        expr: str,
        limit: int = 8,
        output_fields: list[str] | None = None,
    ) -> list[Row]: ...
    async def delete_by_expr(self, collection: str, expr: str) -> int: ...


@runtime_checkable
class EventIndexRepo(_Owned, Protocol):
    """owner: ``capabilities.telemetry``（ES ``dba-*`` 索引写入 + ILM）。

    ★ P1-9/§5.7：``dynamic: strict`` 下写入不匹配会失败 → **必须落本地 DLQ** 并计数。
    """

    owner: ClassVar[str] = "capabilities.telemetry"

    async def ensure_indices(self) -> None: ...
    async def index_run_event(self, doc: Row) -> None: ...
    async def index_sql_audit(self, doc: Row) -> None: ...
    async def index_metric_raw(self, doc: Row) -> None: ...
    async def search(self, alias: str, body: Row) -> list[Row]: ...


@runtime_checkable
class ObjectStoreRepo(_Owned, Protocol):
    """owner: 按 bucket（``modules.chatbi`` / worker / ``capabilities.embedding``）。"""

    owner: ClassVar[str] = "modules.chatbi"

    async def ensure_buckets(self) -> None: ...
    async def put(
        self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str: ...
    async def presigned_get(self, bucket: str, key: str, *, expires_s: int = 3600) -> str: ...
    async def delete(self, bucket: str, key: str) -> None: ...


#: 供单测断言「每个 Protocol 都有 owner」以及 banned-api 规则使用
def _protocol_owners() -> dict[str, str]:
    owners: dict[str, str] = {}
    for obj in list(globals().values()):
        if isinstance(obj, type):
            name = getattr(obj, "owner", None)
            if isinstance(name, str) and name:
                owners[obj.__name__] = name
    return owners


PROTOCOL_OWNERS: dict[str, str] = _protocol_owners()
_ = ExecMeta  # 供类型引用，避免未使用告警
