"""ES 索引与 ILM 规格（§5.7）。

3 个索引（全部 ``dynamic: strict``）：
  1. ``{prefix}-run-event-*``   运行事件（span / 状态流转）
  2. ``{prefix}-sql-audit-*``   SQL 审计全文
  3. ``{prefix}-metric-raw-*``  原始指标点（预聚合前）

ILM（照抄 v1 会怎样错）：v1 的冷阶段用 ``searchable_snapshot``——它需要单独的
可搜索快照存储且**不可再直接查询**，与「审计需可回溯查」冲突。v2 冷阶段改为
``freeze``（冻结索引仍可被 ``_search`` 查询，代价是写入被禁止）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["IndexSpec", "INDEX_SPECS", "ILM_POLICY_NAME", "build_ilm_policy"]

#: ILM 策略名（三索引共用，按 ``policy`` 绑定到模板）
ILM_POLICY_NAME = "dba-retention"


@dataclass(frozen=True)
class IndexSpec:
    """一个索引的物理规格。"""

    suffix: str
    owner: str
    description: str
    properties: dict[str, object]
    #: 热阶段滚动阈值
    rollover_max_age: str = "7d"
    rollover_max_size: str = "30gb"
    #: 保留期（删除阶段）
    retention: str = "365d"


def _keyword() -> dict[str, object]:
    return {"type": "keyword"}


def _long() -> dict[str, object]:
    return {"type": "long"}


def _date() -> dict[str, object]:
    return {"type": "date", "format": "strict_date_optional_time||epoch_millis"}


INDEX_SPECS: tuple[IndexSpec, ...] = (
    IndexSpec(
        suffix="run-event",
        owner="capabilities.telemetry",
        description="运行事件（span / 状态流转）",
        properties={
            "trace_id": _keyword(),
            "span_id": _keyword(),
            "parent_span_id": _keyword(),
            "name": _keyword(),
            "kind": _keyword(),
            "module": _keyword(),
            "status": _keyword(),
            "biz_line_id": _long(),
            "agent_uid": _keyword(),
            "start_ms": _long(),
            "duration_ms": _long(),
            "error_type": _keyword(),
            "error_message": {"type": "text"},
            "@timestamp": _date(),
        },
        retention="180d",
    ),
    IndexSpec(
        suffix="sql-audit",
        owner="modules.chatbi",
        description="SQL 安全审计全文",
        properties={
            "trace_id": _keyword(),
            "user_id": _long(),
            "biz_line_id": _long(),
            "sql_fingerprint": _keyword(),
            "sql_text": {"type": "text"},
            "rewritten_sql": {"type": "text"},
            "dialect": _keyword(),
            "decision": _keyword(),
            "deny_reason": {"type": "text"},
            "guard_stage": _keyword(),
            "scope_injected": {"type": "boolean"},
            "scope_hash": _keyword(),
            "rows_returned": _long(),
            "exec_ms": _long(),
            "@timestamp": _date(),
        },
        retention="365d",
    ),
    IndexSpec(
        suffix="metric-raw",
        owner="modules.observability",
        description="原始指标点（预聚合前）",
        properties={
            "metric": _keyword(),
            "biz_line_id": _long(),
            "agent_uid": _keyword(),
            "model": _keyword(),
            "value": {"type": "double"},
            "unit": _keyword(),
            "@timestamp": _date(),
        },
        rollover_max_age="1d",
        retention="90d",
    ),
)


def build_ilm_policy(spec: IndexSpec) -> dict[str, object]:
    """构造 ILM 策略体：hot(rollover) → warm → cold(``freeze``) → delete。"""
    return {
        "policy": {
            "phases": {
                "hot": {
                    "actions": {
                        "rollover": {
                            "max_age": spec.rollover_max_age,
                            "max_size": spec.rollover_max_size,
                        }
                    }
                },
                "warm": {"min_age": "7d", "actions": {"forcemerge": {"max_num_segments": 1}}},
                # ★ 冷阶段用 freeze：仍可查询；v1 的 searchable_snapshot 不可再查
                "cold": {"min_age": "30d", "actions": {"freeze": {}}},
                "delete": {"min_age": spec.retention, "actions": {"delete": {}}},
            }
        }
    }
