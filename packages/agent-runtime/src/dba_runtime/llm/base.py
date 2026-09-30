"""LLM 客户端 Protocol（内核 L2，◆建议签名）。

对齐《设计方案 v2》§6.1.5 与《实现要点清单》§5.7。

设计原则：**只依赖 OpenAI 兼容协议**；换 Qwen / DeepSeek / GLM 时上层零改动。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from dba_runtime.router import LLMResult, ModelChoice

__all__ = ["LLMClient"]


@runtime_checkable
class LLMClient(Protocol):
    """LLM 适配器。实现放在内核 ``llm/``（openai / anthropic / fake）。"""

    provider: str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        *,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> LLMResult: ...

    def stream(
        self, messages: list[dict[str, Any]], choice: ModelChoice, **kwargs: Any
    ) -> AsyncIterator[str]: ...
