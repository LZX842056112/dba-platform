"""注册中心（内核 L2）。

对齐《设计方案 v2》§6.1.3 / 《实现要点清单》§5.3、U7。

★ U7：``AgentRegistry.register(name, agent)`` / ``.get(name) -> Agent``；``ToolRegistry``
同理；``Tool`` Protocol 为 ``name: str`` + ``async run(args, ctx) -> ToolResult``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from .agent import Agent  # noqa: F401  # 仅用于类型参数/标注
    from .tools.base import Tool  # noqa: F401  # 仅用于类型参数/标注

__all__ = ["AgentRegistry", "ToolRegistry", "Registry"]

T = TypeVar("T")


class Registry(Generic[T]):  # noqa: UP046  # 需运行期 Generic 以便 base class 用字符串前向引用
    """极简命名注册表：register / get / has / names。

    设计取舍：只做「按名字取实现」，不做依赖解析、不做生命周期托管——
    那属于 ``dba.di``（L1 装配层），内核保持零依赖、可单测。
    """

    __slots__ = ("_items", "_kind")

    def __init__(self, kind: str = "item") -> None:
        self._items: dict[str, T] = {}
        self._kind = kind

    def register(self, name: str, item: T) -> T:
        """注册；同名重复注册视为错误（避免静默覆盖）。"""
        if name in self._items:
            raise ValueError(f"{self._kind} '{name}' 已注册，禁止重复注册")
        self._items[name] = item
        return item

    def get(self, name: str) -> T:
        """按名取实现；不存在时抛 KeyError（附带可用项，便于排障）。"""
        try:
            return self._items[name]
        except KeyError as exc:  # noqa: PERF203
            available = ", ".join(sorted(self._items)) or "<empty>"
            raise KeyError(f"{self._kind} '{name}' 未注册；已注册：{available}") from exc

    def has(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        return sorted(self._items)


class AgentRegistry(Registry["Agent"]):
    """Agent 注册中心。"""

    def __init__(self) -> None:
        super().__init__(kind="agent")


class ToolRegistry(Registry["Tool"]):
    """工具注册中心。"""

    def __init__(self) -> None:
        super().__init__(kind="tool")
