"""可编程的 LLM 测试替身（内核 L2）。

用于内核单测：**不连任何数据库 / 网络**即可驱动完整 Pipeline 跑通。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from dba_runtime.router import LLMResult, ModelChoice

__all__ = ["FakeLLMClient"]


class FakeLLMClient:
    """测试替身。``responses`` 按调用次序返回；耗尽后返回 ``default_text``。

    ``stream_chunks`` 控制 ``stream()`` 的分片；未显式指定时按 ``default_text`` 逐字符切分。
    ``fail_with`` + ``fail_times`` 可模拟「前 N 次调用失败」，用于驱动 Pipeline 的回退分支。
    """

    provider = "fake"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        default_text: str = "ok",
        stream_chunks: list[str] | None = None,
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
        cached_tokens: int = 0,
        fail_with: Exception | None = None,
        fail_times: int = 0,
    ) -> None:
        self.responses: list[str] = list(responses or [])
        self.default_text = default_text
        self.stream_chunks = list(stream_chunks or [])
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens
        self.fail_with = fail_with
        self.fail_times = fail_times
        self.calls: list[list[dict[str, Any]]] = []
        self.streamed: list[list[dict[str, Any]]] = []

    def _next_text(self) -> str:
        if self.responses:
            return self.responses.pop(0)
        return self.default_text

    async def complete(
        self,
        messages: list[dict[str, Any]],
        choice: ModelChoice,
        *,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        _ = timeout_s, kwargs
        self.calls.append(messages)
        if self.fail_with is not None and self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with
        return LLMResult(
            text=self._next_text(),
            model=choice.model,
            provider=self.provider,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cached_tokens=self.cached_tokens,
            latency_ms=1,
            finish_reason="stop",
        )

    async def stream(
        self, messages: list[dict[str, Any]], choice: ModelChoice, **kwargs: Any
    ) -> AsyncIterator[str]:
        """异步生成器：逐 chunk yield（模拟打字机）。"""
        _ = choice, kwargs
        self.streamed.append(messages)
        chunks = self.stream_chunks or list(self._next_text())
        for chunk in chunks:
            yield chunk
