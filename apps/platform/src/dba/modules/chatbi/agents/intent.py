"""① ``IntentAgent``：意图解析（查询 / 对比 / 归因 / 预测）+ 实体与时间范围抽取。

对齐《设计方案 v2》§6.2 与《实现要点清单》§5.9（步骤表）。

★ 设计取舍：默认走**确定性规则**（离线、可测、零 token）。原因：
  意图分类是「低价值高频」调用，交给 LLM 既慢又贵；规则命中率不足时
  才由外部注入的 ``router`` 兜底（可选）。
"""

from __future__ import annotations

import re
from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

__all__ = ["IntentAgent"]

#: 意图关键词（从具体到宽泛）
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("compare", ("对比", "比较", "环比", "同比", "差异", "vs", "versus")),
    ("attribution", ("为什么", "原因", "归因", "异常", "波动", "下降", "下降原因")),
    ("predict", ("预测", "预估", "趋势外推", "预计")),
)

#: 区域 / 渠道词典（抽取实体用；可被语义层覆盖）
_REGIONS = ("华东", "华南", "华北", "华中", "西南", "西北", "东北")
_CHANNELS = ("线上", "线下", "门店", "APP", "小程序", "天猫", "京东", "抖音")

#: 时间范围词典
_TIME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("today", ("今天", "今日")),
    ("yesterday", ("昨天", "昨日")),
    ("last_7d", ("近7天", "近七天", "最近一周", "近一周")),
    ("last_30d", ("近30天", "近三十天", "最近一个月")),
    ("last_month", ("上月", "上个月", "last month")),
    ("this_month", ("本月", "这个月", "当月")),
    ("last_quarter", ("上季度", "上个季度")),
    ("ytd", ("年初至今", "YTD", "今年以来")),
)


class IntentAgent:
    """意图解析（无状态）。"""

    name = "intent"

    def __init__(self, router: Any | None = None) -> None:
        #: 可选 LLM 兜底（规则未命中时使用）；默认 None → 纯规则
        self._router = router

    @traced("step.intent", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        question = str(payload.get("question") or "").strip()
        intent = self._classify(question)
        data = {
            "intent": intent,
            "entities": self._entities(question),
            "time_range": self._time_range(question),
            "raw_question": question,
            "question": question,
        }
        return AgentOutput(data=data, confidence=0.9 if intent != "query" else 0.7)

    @staticmethod
    def _classify(question: str) -> str:
        lowered = question.lower()
        for intent, keywords in _INTENT_RULES:
            if any(kw.lower() in lowered for kw in keywords):
                return intent
        return "query"

    @staticmethod
    def _entities(question: str) -> dict[str, list[str]]:
        entities: dict[str, list[str]] = {}
        regions = [r for r in _REGIONS if r in question]
        channels = [c for c in _CHANNELS if c in question]
        if regions:
            entities["region"] = regions
        if channels:
            entities["channel"] = channels
        quoted = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,20})[\"'“”‘’]", question)
        if quoted:
            entities["terms"] = quoted
        return entities

    @staticmethod
    def _time_range(question: str) -> str | None:
        for label, keywords in _TIME_RULES:
            if any(kw in question for kw in keywords):
                return label
        return None
