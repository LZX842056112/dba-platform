"""Agent 上报 token 校验。

独立成模块，避免单测引入 FastAPI、存储驱动等重依赖。
"""

from __future__ import annotations

import secrets

__all__ = ["verify_agent_token"]


def verify_agent_token(provided: str | None, expected: str) -> str:
    """校验 ``X-Agent-Token``。

    未配置期望值时 fail-closed。比较使用常量时间函数。
    """
    if not expected:
        raise ValueError("未配置 DBA_AGENT_TOKEN")
    if not provided or not secrets.compare_digest(provided, expected):
        raise ValueError("X-Agent-Token 无效")
    return provided
