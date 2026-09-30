"""③ ``SqlGeneratorAgent``：SQL 生成（支持带反馈重生成）。

对齐《设计方案 v2》§6.2 与《实现要点清单》§5.9。

★ 省 token 的重生成（照抄 v1 会怎样错）
-------------------------------------
自愈回退时 v1 把**整个上下文**重喂一遍；v2 只把「失败的 SQL + 错误信息」拼进对话
（``_build_retry_messages``），其余上下文沿用首轮的 system prompt——
这也正是「回退只回填失败产物、不重传整包」在生成侧的对偶实现。
"""

from __future__ import annotations

import re
from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

from ..prompts import load_prompt

__all__ = ["SqlGeneratorAgent", "extract_sql"]

_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_START = re.compile(r"\b(select|with)\b", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """从模型输出中抽取 SQL（优先代码块；否则取首个 SELECT/WITH 起）。"""
    raw = (text or "").strip()
    fenced = _SQL_FENCE.search(raw)
    if fenced:
        return fenced.group(1).strip().rstrip(";").strip()
    match = _SQL_START.search(raw)
    if match:
        return raw[match.start() :].strip().rstrip(";").strip()
    return raw.rstrip(";").strip()


class SqlGeneratorAgent:
    """SQL 生成（无状态）。"""

    name = "sql_gen"

    def __init__(self, router: Any, memory: Any | None = None, skill: Any | None = None) -> None:
        self._router = router
        self._memory = memory
        self._skill = skill

    @traced("step.sql_gen", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        feedback = payload.get("_feedback")
        if feedback:
            messages = self._build_retry_messages(payload, feedback)
        else:
            messages = self._build_initial_messages(payload)

        choice = await self._router.route(task="sql_gen", ctx=ctx)
        result = await self._router.chat(messages, choice, ctx)
        sql = extract_sql(str(result.text))

        if ctx.emitter is not None:
            await ctx.emitter.emit(
                "sql.generated",
                {
                    "sql": sql,
                    "metrics": [m.get("metric_code") for m in payload.get("metrics", [])],
                    "model": choice.model,
                },
            )
        return AgentOutput(data={"sql": sql}, confidence=self._confidence(result))

    # ── 消息构造 ─────────────────────────────────────────────────
    @staticmethod
    def _build_initial_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
        template = load_prompt("sql_gen") or "你是资深数据分析师，只写 MySQL 只读 SQL。"
        schema_prompt = str(payload.get("schema_prompt") or "")
        context = schema_prompt.strip() or _summarize(payload)
        return [
            {"role": "system", "content": template},
            {"role": "user", "content": context},
        ]

    @staticmethod
    def _build_retry_messages(
        payload: dict[str, Any], feedback: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """★ 只拼「失败的 SQL + 错误」，不重喂完整上下文（省 token）。"""
        template = load_prompt("sql_gen") or "你是资深数据分析师，只写 MySQL 只读 SQL。"
        failed_sql = str(payload.get("sql") or "")
        error_code = str(feedback.get("error_code") or "")
        error_message = str(feedback.get("error_message") or "")
        user = (
            "上一版 SQL 未通过校验/执行，请只依据下面的错误修正它，\n"
            "不要重述问题背景，也不要输出多余解释。\n\n"
            f"失败 SQL：\n```sql\n{failed_sql}\n```\n\n"
            f"错误码：{error_code}\n错误信息：{error_message}\n"
        )
        return [
            {"role": "system", "content": template},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _confidence(result: Any) -> float | None:
        finish = getattr(result, "finish_reason", None)
        return 0.8 if finish in (None, "stop") else 0.5


def _summarize(payload: dict[str, Any]) -> str:
    """schema_prompt 缺失时的最小上下文（口径 + 表 + 问题）。"""
    metrics = payload.get("metrics") or []
    tables = payload.get("tables") or []
    question = payload.get("question") or ""
    metric_txt = ", ".join(str(m.get("metric_code")) for m in metrics) or "（无）"
    return (
        f"## 指标口径\n{metric_txt}\n\n"
        f"## 可访问表\n{', '.join(map(str, tables)) or '（无）'}\n\n"
        f"## 用户问题\n{question}"
    )
