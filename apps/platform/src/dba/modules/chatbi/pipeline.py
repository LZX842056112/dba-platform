"""ChatBI **七步流水线**（六角色 + ``sql_exec``）。

对齐《设计方案 v2》§6.2 / §8.1 / §8.2 与《实现要点清单》§5.9（步骤表，冻结）。

★ v2 的关键修正（照抄 v1 会怎样错）
----------------------------------
v1 的 ``sql_guard.next`` 直接指向 ``visual``——**真实取数那一步不在流水线里**：
没有 Step、没有超时、没有回退策略、没有 span。而它恰恰是整条链路上风险最高的一段
（锁等待、连接中断、慢查询）。v2 插入 ``sql_exec`` 步骤。

★ ``sql_exec`` 的回退语义口径：``goto`` **只应对 ``SQL_EXEC_TRANSIENT``**
  （超时 / 锁等待 / 连接中断）生效；列名不存在、语法错误、权限不足都在 ``sql_guard``
  阶段被拦下或分类，不允许走到执行阶段再回退——否则「重试」会变成刷 token 的放大器。
  越权（``SQL_PERMISSION_DENIED``）由内核 Pipeline **短路、不重试**。
  ⚠ 已知边界：内核 Pipeline（B1 冻结）对 ``on_error="goto"`` 仅在越权上短路，
  其余错误最多触发 1 次 ``goto``（受 ``max_retries`` 约束，不会无限放大）。
"""

from __future__ import annotations

from typing import Any

from dba_runtime.errors import classify
from dba_runtime.pipeline import Pipeline, Step
from dba_runtime.registry import AgentRegistry

__all__ = ["CHATBI_STEPS", "build_chatbi_pipeline"]

# ── §6.2 步骤表（冻结）────────────────────────────────────────────────
CHATBI_STEPS: list[Step] = [
    Step(
        name="intent",
        agent_name="intent",
        next="schema_link",
        timeout_s=15,
        on_error="retry",
        max_retries=1,
    ),
    Step(
        name="schema_link",
        agent_name="schema_link",
        next="sql_gen",
        timeout_s=30,
        on_error="goto",
        goto_step="intent",
        max_retries=1,
    ),
    Step(
        name="sql_gen",
        agent_name="sql_gen",
        next="sql_guard",
        timeout_s=60,
        on_error="retry",
        max_retries=2,
        produces=("sql",),
    ),
    Step(
        name="sql_guard",
        agent_name="sql_guard",
        next="sql_exec",
        timeout_s=20,
        on_error="goto",
        goto_step="sql_gen",
        max_retries=2,
        produces=("sql", "scope_injected"),
    ),
    Step(
        name="sql_exec",
        agent_name="sql_exec",
        next="visual",
        timeout_s=30,
        on_error="goto",
        goto_step="sql_gen",
        max_retries=1,
        produces=("rows", "row_count"),
        on_timeout="goto",
    ),
    Step(
        name="visual",
        agent_name="visual",
        next="narrator",
        timeout_s=45,
        on_error="retry",
        max_retries=1,
    ),
    Step(
        name="narrator",
        agent_name="narrator",
        next=None,
        timeout_s=45,
        on_error="fail",
    ),
]


def build_chatbi_pipeline(registry: AgentRegistry) -> Pipeline:
    """构建 ChatBI 七步流水线，并挂上「失败留痕」钩子。"""
    pipeline = Pipeline("chatbi", CHATBI_STEPS, entry="intent", registry=registry)
    pipeline.on("error", _record_failure)
    return pipeline


async def _record_failure(
    state: str, data: dict[str, Any], ctx: Any, exc: Exception | None
) -> None:
    """把失败信息记进数据包 ``_last_failure``。

    用途：
    * ``SqlGeneratorAgent`` 从 ``_feedback`` 读「失败 SQL + 错误」做带反馈重生成；
    * ``SqlGuardAgent`` 依据 ``_last_failure`` 判断「本次是通过护栏的重生成结果」，
      从而发出 ``sql.retry.resolved``（§9.3 唯一生产者）。
    """
    _ = ctx
    if exc is None:
        return
    symbol = classify(exc)[0]
    data["_last_failure"] = {
        "failed_step": state,
        "error_code": symbol,
        "message": str(exc),
    }
