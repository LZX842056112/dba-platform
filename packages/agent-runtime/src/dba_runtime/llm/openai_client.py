"""OpenAI 兼容适配器（内核 L2）。

对齐《设计方案 v2》§6.1.5、§3.1。

★ 依赖策略：内核 ``dba-runtime`` **故意不声明** ``openai`` 依赖（只依赖
pydantic + opentelemetry-api + 标准库，见 §4.4）。因此本模块在**方法内部**惰性导入
``openai``——仅导入本模块不会失败，只有真正实例化适配器时才需要 ``dba[llm]`` extra。
这样 ``import dba_runtime`` 在任何环境下都成立。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from dba_runtime.router import LLMResult, ModelChoice

__all__ = ["OpenAIClient"]


def _require_openai() -> Any:
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 取决于是否安装 extra
        raise RuntimeError(
            "使用 OpenAIClient 需要安装 `dba[llm]`（openai / anthropic / tiktoken）"
        ) from exc
    return openai


class OpenAIClient:
    """OpenAI 兼容客户端（亦可指向 Qwen / DeepSeek / GLM 的兼容端点）。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str = "openai",
        timeout_s: float = 60.0,
    ) -> None:
        openai = _require_openai()
        self.provider = provider
        self._timeout_s = timeout_s
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        *,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        started = time.perf_counter()
        resp = await self._client.chat.completions.create(
            model=choice.model,
            messages=messages,
            max_tokens=choice.max_tokens,
            temperature=choice.temperature,
            timeout=timeout_s or self._timeout_s,
            **kwargs,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        # ★ 归一化：prompt_tokens 定义为「含缓存命中部分」；cached 从 details 提取。
        cached_tokens = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        text = resp.choices[0].message.content or ""
        return LLMResult(
            text=text,
            model=choice.model,
            provider=self.provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            latency_ms=latency_ms,
            finish_reason=getattr(resp.choices[0], "finish_reason", None),
            usage_source="measured",
        )

    async def stream(
        self, messages: list[dict[str, Any]], choice: ModelChoice, **kwargs: Any
    ) -> AsyncIterator[str]:
        stream = self._client.chat.completions.create(
            model=choice.model,
            messages=messages,
            max_tokens=choice.max_tokens,
            temperature=choice.temperature,
            stream=True,
            timeout=self._timeout_s,
            **kwargs,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta
