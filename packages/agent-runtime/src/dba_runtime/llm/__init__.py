"""LLM 适配器包（内核 L2）。"""

from __future__ import annotations

from dba_runtime.llm.anthropic_client import AnthropicClient
from dba_runtime.llm.base import LLMClient
from dba_runtime.llm.fake import FakeLLMClient
from dba_runtime.llm.openai_client import OpenAIClient
from dba_runtime.router import LLMResult, ModelChoice

__all__ = [
    "LLMClient",
    "LLMResult",
    "ModelChoice",
    "FakeLLMClient",
    "OpenAIClient",
    "AnthropicClient",
]
