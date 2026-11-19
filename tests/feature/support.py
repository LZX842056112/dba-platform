"""特征测试的替身与夹具。

所有替身只在内存中工作。不读取环境变量，不连接外部服务。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from dba_runtime.agent import AgentOutput
from dba_runtime.context import RunContext


def make_ctx(**overrides: Any) -> RunContext:
    """构造测试用 RunContext。"""
    values: dict[str, Any] = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "module": "chatbi",
        "user_id": 1,
        "biz_line_id": 1,
    }
    values.update(overrides)
    return RunContext(**values)


class FakeAuditRepo:
    """记录审计调用，不落库。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def write(self, **kwargs: Any) -> None:
        self.records.append(dict(kwargs))

    async def mark_truncated(self, *args: Any, **kwargs: Any) -> None:
        self.records.append({"mark_truncated": {"args": args, "kwargs": kwargs}})


class FakeScopeRuleRepo:
    """返回预置的行级权限规则。"""

    def __init__(self, rules: list[dict[str, Any]], version: str = "v1") -> None:
        self.rules = rules
        self.version = version

    async def rules_for_roles(self, role_ids: list[int]) -> list[dict[str, Any]]:
        return list(self.rules)

    async def rule_version(self, role_ids: list[int]) -> str:
        return self.version


class FakeMappingRepo:
    """返回预置的字段映射。空列表表示没有 join 路径。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    async def all_mappings(self) -> list[dict[str, Any]]:
        return list(self.rows)


class FakeReadOnlyConnection:
    """只读连接替身。可配置 explain 抛错。"""

    def __init__(self, error: Exception | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.error = error
        self.rows = rows or []

    async def explain(self, sql: str) -> None:
        if self.error is not None:
            raise self.error

    async def fetch(self, sql: str, max_rows: int = 5000) -> tuple[list[dict[str, Any]], bool]:
        return list(self.rows), False


class FakeAgent:
    """实现内核 Agent 协议的记录型 Agent。"""

    def __init__(self, name: str, handler: Callable[[dict[str, Any], RunContext], Awaitable[AgentOutput]]) -> None:
        self.name = name
        self.handler = handler

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        return await self.handler(payload, ctx)


def ok_handler(name: str) -> Callable[[dict[str, Any], RunContext], Awaitable[AgentOutput]]:
    """正常路径：记录步骤名并返回成功。"""

    async def run(payload: dict[str, Any], ctx: RunContext) -> AgentOutput:
        payload.setdefault("steps", []).append(name)
        return AgentOutput(data={name: True})

    return run


def build_registry(overrides: dict[str, Callable] | None = None):
    """按 CHATBI_STEPS 注册 7 个替身 Agent。"""
    from dba.modules.chatbi.pipeline import CHATBI_STEPS
    from dba_runtime.registry import AgentRegistry

    handlers = {step.agent_name: ok_handler(step.agent_name) for step in CHATBI_STEPS}
    handlers.update(overrides or {})
    registry = AgentRegistry()
    for name, handler in handlers.items():
        registry.register(name, FakeAgent(name, handler))
    return registry
