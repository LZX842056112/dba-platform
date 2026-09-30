"""⑥ ``NarratorAgent``：结论解读（★ 流式 + 显式报口径）。

对齐《设计方案 v2》§6.2 与《实现要点清单》§5.9（3.8）。

★ 必须流式（照抄 v1 会怎样错）
------------------------------
v1 的事件表里有 ``narration.delta``，但 Agent 既拿不到 ``emitter`` 也没有流式回调，
「打字机效果」在架构上**不可能实现**。v2 用 ``router.stream`` + ``ctx.emitter`` 落地。

★ 必须显式报口径（"本项目 GMV 为支付口径、不含税"），
  否则业务人员无从判断数字是否可比；若结果被截断也必须说明。
"""

from __future__ import annotations

from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

from ..prompts import load_prompt

__all__ = ["NarratorAgent"]


class NarratorAgent:
    """结论解读（无状态）。"""

    name = "narrator"

    def __init__(self, router: Any) -> None:
        self._router = router

    @traced("step.narrator", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        choice = await self._router.route(task="narration", ctx=ctx)
        chunks: list[str] = []
        async for delta in self._router.stream(self._messages(payload), choice, ctx):
            chunks.append(delta)
            if ctx.emitter is not None:
                await ctx.emitter.emit("narration.delta", {"text_delta": delta})
        return AgentOutput(data={"narration": "".join(chunks)})

    @staticmethod
    def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
        template = load_prompt("narrator") or "把查询结果翻译成自然语言结论，并显式报出口径。"
        metrics = payload.get("metrics") or []
        caliber = "; ".join(
            f"{m.get('metric_name')}（{m.get('caliber')}）" for m in metrics if m.get("caliber")
        )
        truncated = bool(payload.get("truncated"))
        rows = payload.get("rows") or []
        return [
            {"role": "system", "content": template},
            {
                "role": "user",
                "content": (
                    f"## 用户问题\n{payload.get('question') or ''}\n\n"
                    f"## 口径\n{caliber or '（未登记口径，须说明）'}\n\n"
                    f"## 是否截断\n{'是（结果超过上限，已截断）' if truncated else '否'}\n\n"
                    f"## 结果样例（前 10 行，共 {len(rows)} 行）\n{rows[:10]}"
                ),
            },
        ]
