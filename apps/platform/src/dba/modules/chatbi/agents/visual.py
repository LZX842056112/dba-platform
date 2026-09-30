"""⑤ ``VisualAgent``：可视化编排（技能确定性路径 / LLM 路径）。

对齐《设计方案 v2》§6.2 与《实现要点清单》§5.9（3.7）。

★ 逐面板下发（照抄 v1 会怎样错）
------------------------------
v1 的事件表里定义了 ``dashboard.spec.delta``，但 Agent 签名里没有 ``emitter``——
这个事件**没有任何可能的发送者**。v2 把 ``emitter`` 放进 ``RunContext``，
事件才真正可发；由此第一块图渲染完成时第二块还在生成，感知延迟大幅降低。

★ pydantic v2 里 ``.dict()`` 已废弃，统一 ``model_dump()``。
"""

from __future__ import annotations

from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext
from dba_runtime.telemetry import traced

from ..prompts import load_prompt
from ..spec_builder import DashboardSpecBuilder

__all__ = ["VisualAgent"]


class VisualAgent:
    """可视化编排（无状态）。"""

    name = "visual"

    def __init__(
        self, router: Any, spec_builder: DashboardSpecBuilder, skill: Any | None = None
    ) -> None:
        self._router = router
        self._spec_builder = spec_builder
        self._skill = skill

    @traced("step.visual", kind="agent")
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        # 命中技能模板 → 确定性路径（省 token，且结果稳定）
        skill_spec = payload.get("_skill_spec")
        if skill_spec:
            spec = self._spec_builder.from_skill(dict(skill_spec), payload, ctx)
        else:
            choice = await self._router.route(task="visual_layout", ctx=ctx)
            raw = await self._router.chat(self._messages(payload), choice, ctx)
            spec = self._spec_builder.from_llm_json(str(raw.text), payload, ctx)

        spec = await self._spec_builder.materialize(spec, payload)

        # ★ 逐面板下发
        for panel in spec.panels:
            if ctx.emitter is not None:
                await ctx.emitter.emit("dashboard.spec.delta", {"panels": [panel.model_dump()]})
        if ctx.emitter is not None:
            await ctx.emitter.emit(
                "dashboard.spec.ready",
                {
                    "dashboard_id": spec.dashboard_id,
                    "version": spec.version,
                    "layout": spec.layout.model_dump(),
                },
            )
        return AgentOutput(data={"dashboard_spec": spec.model_dump()})

    @staticmethod
    def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
        template = load_prompt("visual") or "根据查询结果产出大屏 JSON 布局。"
        rows = payload.get("rows") or []
        columns = payload.get("columns") or []
        sample = rows[:5]
        return [
            {"role": "system", "content": template},
            {
                "role": "user",
                "content": (
                    f"## 用户问题\n{payload.get('question') or ''}\n\n"
                    f"## 结果列\n{columns}\n\n"
                    f"## 结果样例（前 5 行，共 {len(rows)} 行）\n{sample}"
                ),
            },
        ]
