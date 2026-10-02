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

from dba.util.jsonx import loads_object

from ..prompts import load_prompt

__all__ = ["SqlGeneratorAgent", "extract_sql", "parse_sql_payload"]

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


def _strip_fence(text: str) -> str:
    """剥离 ```json 围栏，取第一个 ``{`` 起始的块。"""
    if "```" not in text:
        return text
    for part in text.split("```"):
        candidate = part.strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()
        if candidate.startswith("{"):
            return candidate
    return text


def parse_sql_payload(text: str) -> tuple[str, list[dict[str, Any]] | None]:
    """解析模型输出 → ``(主 SQL, 多查询列表或 None)``。

    支持两种形态：① **多查询信封** ``{"sql": "...", "queries": [{"ref","sql"}, ...]}``；
    ② 裸 SQL（或 ```sql 围栏）——此时 ``queries`` 为 ``None``，走原有单查询路径。

    输入：模型返回的原始文本（可能夹带 ```json 围栏 / 解释文字）。
    输出：主查询 SQL + 可选的 ``queries`` 列表（每个元素含 ``ref`` 与 ``sql``）。
    注意：信封里 ``queries`` 全为空时不当作多查询，退化为「提取单条 SQL」。
    """
    candidate = _strip_fence((text or "").strip())
    if candidate.startswith("{"):
        # ★ 统一走 loads_object：内部已实现「整体解析 → 花括号子串」两级降级，
        #   避免各 Agent 自行重复写 try/except JSONDecodeError。
        obj = loads_object(candidate)
        if isinstance(obj, dict) and isinstance(obj.get("queries"), list):
            queries = [
                dict(q) for q in obj["queries"] if isinstance(q, dict) and str(q.get("sql") or "")
            ]
            if queries:
                sql = str(obj.get("sql") or queries[0]["sql"])
                return sql, queries
    return extract_sql(text), None


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
        sql, queries = parse_sql_payload(str(result.text))

        if ctx.emitter is not None:
            await ctx.emitter.emit(
                "sql.generated",
                {
                    "sql": sql,
                    "metrics": [m.get("metric_code") for m in payload.get("metrics", [])],
                    "model": choice.model,
                },
            )
        data: dict[str, Any] = {"sql": sql}
        if queries:
            data["queries"] = queries
        return AgentOutput(data=data, confidence=self._confidence(result))

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
