"""RunContext —— 无侵入埋点的基石（内核 L2，零外部依赖）。

本模块实现《设计方案 v2》§6.1.1 + 架构师《实现要点清单》§5.1 的冻结签名，
并落地三条 ★ v2 修法与红线 5 / P0-9。

为什么这样写（照抄 v2 正文会怎样错）
--------------------------------------
1) **不可变必须覆盖嵌套可变对象**：v1 的 ``extra`` 是普通 ``dict``，
   ``frozen=True`` 只挡住 ``ctx.extra = {...}``，挡不住 ``ctx.extra["k"] = v``；
   更隐蔽的是 ``dataclasses.replace()`` 是**浅拷贝**，``with_quality()`` 产出的新
   上下文与父上下文**共享同一个 dict 对象**。于是预算守卫执行
   ``new_ctx.extra["_downgrade_count"] = ...`` 时会把父上下文一起改掉——
   同一个 Run 的兄弟分支互相污染，``max_downgrades_per_run`` 直接失效。
   本模块把 ``extra`` 收敛为只读 ``MappingProxyType``，并规定所有派生只能经 ``with_*``
   方法（每个方法各自复制一份新 dict）。
2) **降级计数提升为显式字段**：``downgrade_count`` / ``downgraded_from`` 是独立字段，
   由 ``with_downgrade()`` 派生出新对象，天然不存在跨分支污染（见 with_downgrade）。
3) **emitter 放进上下文**：v1 的 Agent 签名是 ``run(payload, ctx)``，拿不到 Pipeline 的
   emitter，导致 ``dashboard.spec.delta`` / ``narration.delta`` 这类流式事件在架构上
   无法被发出。

红线 5：同一次 Run 内的派生上下文**只能由 ``with_*`` 方法构造**，禁止原地修改
``RunContext``（含 ``extra``）。
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环 import
    from .events import EventEmitter

__all__ = [
    "RunContext",
    "CancelToken",
    "set_current",
    "reset_current",
    "ctx",
    "ctx_or_none",
    "new_trace_id",
    "new_span_id",
    "new_ulid",
]

Quality = Literal["eco", "std", "max"]
ModuleName = Literal["chatbi", "observability", "finops", "system"]


@dataclass(frozen=True, slots=True)
class RunContext:
    """一次调用的上下文。贯穿 Pipeline、Agent、LLM、工具、SQL 执行。

    设计约束（见模块 docstring）：
      * ``frozen=True, slots=True``；
      * ``extra`` 在 ``__post_init__`` 中被包成只读 ``MappingProxyType``；
      * 所有派生走 ``child`` / ``with_quality`` / ``with_downgrade`` / ``with_retry`` /
        ``with_extra``，每个方法返回**新对象**，父子不共享可变对象。
    """

    trace_id: str  # 32 位 hex，全链路唯一
    span_id: str  # 当前 span
    module: ModuleName

    user_id: int | None = None
    biz_line_id: int | None = None
    agent_uid: str | None = None
    session_id: str | None = None

    # ★ 权限指纹：32 位 hex（sha256 前 128bit），参与语义缓存 key 与前端校验
    scope_hash: str | None = None
    # ★ v2：scope 规则版本（取该用户 row_scope_rule 的最大 updated_at）。
    #        权限规则改了但 hash 不变 = 继续命中旧权限缓存，这是必须堵住的漏洞（P0-10）。
    scope_rule_version: str | None = None

    # 需要检查的预算作用域（从具体到宽泛），如 ("AGENT:ag_x", "BIZ_LINE:12", "GLOBAL:*")
    budget_keys: tuple[str, ...] = ()

    # 模型质量档位（预算守卫可下调）
    quality: Quality = "std"

    # 已注入的 prompt 上下文（记忆/技能），供 B 模块统计命中率
    memory_hits: tuple[str, ...] = ()
    memory_lookups: int = 0
    skill_key: str | None = None
    skill_is_reuse: bool = False

    # 回退计数（自愈上限控制）
    retry_count: int = 0

    # ★ v2：降级计数与来源从 extra 提升为显式字段（extra 已只读）
    downgrade_count: int = 0
    downgraded_from: str | None = None

    # 取消信号（超时、熔断、用户主动取消）
    cancel_token: CancelToken | None = None

    # ★ v2：事件发射器。Agent 通过 ctx.emitter 发 run 级事件（逐面板、narration 流式）
    emitter: EventEmitter | None = None

    # ★ v2：整次 Run 的墙钟截止时间（以 time.monotonic() 为基准），用于全局超时
    deadline_at: float | None = None

    # 自由扩展（只读；只能通过 with_extra() 派生）
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # ★ v2 / P0-9：把传进来的 dict 包成只读视图，堵住 ``ctx.extra["k"] = v``。
        # 若传入的已是 MappingProxyType，则保持同一对象（只读映射可安全共享）。
        if not isinstance(self.extra, MappingProxyType):
            object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    # ── 派生方法（红线 5：唯一合法构造入口） ──────────────────────────────
    def child(self, name: str, span_id: str) -> RunContext:
        """派生子 span 的上下文。同一次 Run，不同的 span_id。

        注意 ``extra`` 用 ``{**self.extra, ...}`` 复制成**新 dict**，因此子上下文与父
        上下文不共享可变对象；``replace()`` 本就是浅拷贝，这里显式复制才是关键。
        """
        return replace(self, span_id=span_id, extra={**self.extra, "span_name": name})

    def with_quality(self, quality: Quality) -> RunContext:
        """仅切换质量档位（不改变降级计数）。"""
        return replace(self, quality=quality)

    def with_downgrade(self, quality: Quality, from_quality: str) -> RunContext:
        """★ v2 / U10：降级专用。计数是**显式字段**，不存在跨分支污染。

        v1 写法 ``new_ctx.extra["_downgrade_count"] = ...`` 在 ``extra`` 变成只读
        映射后会直接 ``TypeError``；且即便能写，也是改到了共享对象上，兄弟分支互相污染。
        """
        return replace(
            self,
            quality=quality,
            downgrade_count=self.downgrade_count + 1,
            downgraded_from=from_quality,
        )

    def with_retry(self) -> RunContext:
        """回退计数 +1（自愈上限控制）。"""
        return replace(self, retry_count=self.retry_count + 1)

    def with_extra(self, **kv: Any) -> RunContext:
        """★ v2 / P0-9：扩展 extra 的**唯一入口**（复制成新的只读映射）。"""
        return replace(self, extra={**self.extra, **kv})

    def with_deadline(self, deadline_at: float) -> RunContext:
        """设置整次 Run 的墙钟截止时间（``time.monotonic()`` 基准）。"""
        return replace(self, deadline_at=deadline_at)

    def with_emitter(self, emitter: EventEmitter | None) -> RunContext:
        """注入事件发射器（Pipeline 在入口调用）。"""
        return replace(self, emitter=emitter)

    @property
    def memory_hit_rate(self) -> float:
        """记忆命中率；无查询时返回 0.0（避免除零）。"""
        return (len(self.memory_hits) / self.memory_lookups) if self.memory_lookups else 0.0


class CancelToken:
    """协作式取消。Agent 在耗时操作前应检查并主动退出。

    协作式（而非 ``asyncio.CancelledError`` 强杀）的好处：Agent 可以在中途收尾、
    落账、归还预算预留，不留悬挂状态。
    """

    __slots__ = ("_cancelled", "_reason")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None

    def cancel(self, reason: str) -> None:
        """标记取消（幂等：重复调用保留首次原因）。"""
        self._cancelled = True
        if self._reason is None:
            self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason


# ★ 模块级 ContextVar —— 全代码任意位置都能读到当前上下文。
#   ContextVar 是「任务局部变量」，asyncio.create_task 会复制当前上下文，
#   子任务里 set_current 不回写父任务——这正是我们想要的并发隔离语义。
_CURRENT: ContextVar[RunContext | None] = ContextVar("dba_current_ctx", default=None)


def set_current(ctx: RunContext) -> Token[RunContext | None]:
    """绑定当前上下文，返回可用于还原的 Token。"""
    return _CURRENT.set(ctx)


def reset_current(token: Token[RunContext | None]) -> None:
    """按 Token 还原（必须与 set_current 配对，通常在 finally 中调用）。"""
    _CURRENT.reset(token)


def ctx() -> RunContext:
    """获取当前 RunContext。未设置时抛错（说明调用链脱离了编排层）。"""
    current = _CURRENT.get()
    if current is None:
        raise RuntimeError(
            "RunContext 未设置。所有业务代码必须由 Pipeline 驱动，或显式用 set_current() 包裹。"
        )
    return current


def ctx_or_none() -> RunContext | None:
    """获取当前 RunContext；未设置时返回 None（软读取场景）。"""
    return _CURRENT.get()


# ── 标识符生成工具（标准库实现，无第三方依赖） ──────────────────────────
def new_trace_id() -> str:
    """生成 32 位 hex 的 trace_id。"""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """生成 16 位 hex 的 span_id。"""
    return uuid.uuid4().hex[:16]


# Crockford Base32（ULID 字母表，去掉易混淆的 I/L/O/U）
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(timestamp_ms: int | None = None) -> str:
    """生成 26 字符 ULID（时间有序 + 随机），用作 reservation_id 等幂等键。"""
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    # 48 bit 时间戳 → 10 字符
    chars = []
    for shift in range(9, -1, -1):
        chars.append(_ULID_ALPHABET[(ts >> (5 * shift)) & 0x1F])
    # 80 bit 随机 → 16 字符
    rnd = int.from_bytes(os.urandom(10), "big")
    for shift in range(15, -1, -1):
        chars.append(_ULID_ALPHABET[(rnd >> (5 * shift)) & 0x1F])
    return "".join(chars)
