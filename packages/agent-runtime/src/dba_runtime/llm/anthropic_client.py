"""Anthropic 适配器（内核 L2，[P2]）。

对齐《设计方案 v2》§6.1.5。依赖同样采用**方法内惰性导入**（见 openai_client 说明）。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from dba_runtime.router import LLMResult, ModelChoice

__all__ = ["AnthropicClient"]


def _require_anthropic() -> Any:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 取决于是否安装 extra
        raise RuntimeError(
            "使用 AnthropicClient 需要安装 `dba[llm]`（openai / anthropic / tiktoken）"
        ) from exc
    return anthropic


class AnthropicClient:
    """Anthropic Messages API 适配器。

    注意 Anthropic 的 usage 语义：``input_tokens`` **不含**缓存命中部分，
    与 OpenAI 相反。因此这里把 ``cache_read_input_tokens`` 加回 ``input_tokens``，
    统一为「prompt_tokens 含缓存命中」的内核口径（见 §5.2.3 成本不变量）。
    """

    provider = "anthropic"

    def __init__(self, *, api_key: str | None = None, timeout_s: float = 60.0) -> None:
        anthropic = _require_anthropic()
        self._timeout_s = timeout_s
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        *,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        _ = timeout_s, kwargs
        started = time.perf_counter()
        resp = await self._client.messages.create(
            model=choice.model,
            messages=messages,
            max_tokens=choice.max_tokens,
            temperature=choice.temperature,
            timeout=self._timeout_s,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(resp, "usage", None)
        raw_input = int(getattr(usage, "input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        output = int(getattr(usage, "output_tokens", 0) or 0)
        text = "".join(getattr(block, "text", "") for block in getattr(resp, "content", []) or [])
        return LLMResult(
            text=text,
            model=choice.model,
            provider=self.provider,
            prompt_tokens=raw_input + cache_read,  # 归一化为「含缓存命中」
            completion_tokens=output,
            cached_tokens=cache_read,
            latency_ms=latency_ms,
            finish_reason=getattr(resp, "stop_reason", None),
            usage_source="measured",
        )

    async def stream(
        self, messages: list[dict[str, Any]], choice: ModelChoice, **kwargs: Any
    ) -> AsyncIterator[str]:
        _ = kwargs
        async with self._client.messages.stream(
            model=choice.model,
            messages=messages,
            max_tokens=choice.max_tokens,
            temperature=choice.temperature,
        ) as stream:
            async for text in stream.text_stream:
                yield text
