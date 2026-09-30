"""模块 10 · 死循环检测（§6.5 ``LoopDetector``）。

对齐《设计方案 v2》§6.5 与《实现要点清单》§3.27。

为什么单独做死循环检测
----------------------
「一夜烧掉几个月预算」的元凶几乎都是死循环：同一 Agent 在同一滑动窗口内，用
**相同工具 + 相同参数**反复调用。判据：::

    key   = loop:{agent_uid}:{tool_name}:{sha256(规范化 JSON args)}
    count = incr_with_ttl(key, window_s)
    count >= threshold  →  判定死循环 + 告警

★ 参数指纹用 ``sha256(canonical_json)``：否则「参数只差空格 / 键序不同」会被当成
不同调用，逃避检测。

★ §5.9 归属的一处**已登记文档缺口**：§5.8 的 Redis 键设计里**没有** ``loop:*`` 这个
命名空间，也没有对应的 L4 能力层。本模块为落地该功能，引入 ``LoopCounter`` 抽象
（``RedisLoopCounter`` 落地实现 / ``InMemoryLoopCounter`` 测试用），并在报告「遗留问题」
中登记「``loop:*`` 键未在 §5.8 声明、无 owner 能力层」。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol, runtime_checkable

from dba_runtime import RunContext

__all__ = [
    "args_fingerprint",
    "LoopCounter",
    "InMemoryLoopCounter",
    "RedisLoopCounter",
    "LoopDetector",
]

logger = logging.getLogger("dba.modules.finops.loop_detector")


def args_fingerprint(args: dict[str, Any] | None) -> str:
    """参数指纹：``sha256(规范化 JSON)[:16]``。

    ``sort_keys=True`` + ``separators`` 保证「键序不同 / 空白不同」得到同一指纹。
    """
    canonical = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@runtime_checkable
class LoopCounter(Protocol):
    """滑动窗口计数器（死循环判据的底层）。"""

    async def incr_with_ttl(self, key: str, ttl_s: int) -> int: ...


class InMemoryLoopCounter:
    """进程内计数器（单测 / 无 Redis 时使用；不具备跨副本能力）。"""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def incr_with_ttl(self, key: str, ttl_s: int) -> int:
        _ = ttl_s  # 进程内实现不做真实过期（仅测试语义）
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]


class RedisLoopCounter:
    """Redis 计数器（``INCR`` + ``EXPIRE``，首次计数时设 TTL）。

    ★ 为什么直接拿 ``RedisStorage.client``：Redis 的通用 ``INCR`` 不在 §5.8 的键契约里，
    也没有 L4 能力层封装（见模块 docstring 的文档缺口登记）。这里保持最小实现，
    并在具备正式能力层后替换为「经 L4 调用」。
    """

    def __init__(self, redis: Any, *, prefix: str = "loop") -> None:
        self._redis = redis
        self._prefix = prefix

    async def incr_with_ttl(self, key: str, ttl_s: int) -> int:
        client = getattr(self._redis, "client", self._redis)
        full = f"{self._prefix}:{key}" if not key.startswith(f"{self._prefix}:") else key
        count = int(await client.incr(full))
        if count == 1:
            await client.expire(full, max(1, int(ttl_s)))
        return count


class LoopDetector:
    """★ 死循环检测器。"""

    def __init__(
        self,
        counter: LoopCounter | None = None,
        *,
        window_s: int = 300,
        threshold: int = 8,
        alert_repo: Any = None,
    ) -> None:
        self._counter: LoopCounter = counter or InMemoryLoopCounter()
        self.window_s = window_s
        self.threshold = threshold
        self._alerts = alert_repo

    async def check_and_record(
        self, ctx: RunContext, tool_name: str, args: dict[str, Any] | None = None
    ) -> bool:
        """记录一次工具调用；返回 ``True`` 表示已判定死循环。"""
        agent_uid = ctx.agent_uid or "unknown"
        key = f"{agent_uid}:{tool_name}:{args_fingerprint(args)}"
        count = await self._counter.incr_with_ttl(key, self.window_s)
        if count >= self.threshold:
            await self._raise_loop_alert(ctx, tool_name, count)
            return True
        return False

    async def _raise_loop_alert(self, ctx: RunContext, tool_name: str, count: int) -> None:
        """告警：写 ``alert_event``（category=loop_suspect）+ 发 ``budget.warning`` 事件。"""
        logger.warning(
            "检测到疑似死循环 agent=%s tool=%s count=%d window=%ds",
            ctx.agent_uid,
            tool_name,
            count,
            self.window_s,
        )
        if self._alerts is not None:
            row: dict[str, Any] = {
                "severity": "critical",
                "category": "loop_suspect",
                "scope_type": "AGENT" if ctx.agent_uid else "GLOBAL",
                "scope_id": ctx.agent_uid,
                "metric": "tool_calls",
                "observed_value": float(count),
                "baseline_value": float(self.threshold),
                "attribution_json": {
                    "tool_name": tool_name,
                    "calls": count,
                    "hypothesis": (
                        f"工具 {tool_name} 在 {self.window_s}s 内被以相同参数调用 {count} 次"
                    ),
                },
                "trace_id": ctx.trace_id,
                "status": "open",
            }
            try:
                await self._alerts.insert(row)
            except Exception as exc:  # noqa: BLE001 - 告警写入失败不影响判定结果
                logger.warning("死循环告警写入失败（忽略）：%s", exc)

        emitter = ctx.emitter
        if emitter is not None:
            try:
                await emitter.emit(
                    "budget.warning",
                    {"reason": "loop_suspect", "tool": tool_name, "count": count},
                )
            except Exception:  # noqa: BLE001
                logger.warning("死循环事件发送失败（忽略）", exc_info=True)
