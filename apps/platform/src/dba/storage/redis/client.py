"""Redis 客户端封装（懒加载驱动 + fakeredis 可插拔）。

对齐《设计文档 v2》§3.1 / §5.6。

★ 为什么懒加载：``redis`` 是可选 extra（``dba[redis]``）；本地开发/单测可用
``fakeredis[lua]``（基于 lupa 的真实 Lua 解释器）验证脚本逻辑，无需起真实 Redis。
因此本模块**只在真正构造时** import 驱动，并对两者统一暴露 ``RedisLike`` 协议。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from dba_runtime.errors import StorageUnavailableError

__all__ = ["RedisStorage", "build_redis"]

logger = logging.getLogger("dba.storage.redis")


def build_redis(dsn: str, *, use_fake: bool = False) -> Any:
    """构造 Redis 客户端。

    :param dsn: ``redis://host:port/db``
    :param use_fake: 为 ``True`` 时用 ``fakeredis``（含真实 Lua 支持，供单测）。
    """
    if use_fake:
        try:
            import fakeredis.aioredis  # noqa: PLC0415

            return fakeredis.aioredis.FakeRedis(decode_responses=True)
        except ImportError as exc:  # pragma: no cover
            raise StorageUnavailableError(
                "fakeredis 未安装（`uv sync` 的 dev 组应包含 fakeredis[lua]）",
                detail={"component": "redis"},
            ) from exc
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "Redis 需要安装 `dba[redis]`（redis[hiredis]）", detail={"component": "redis"}
        ) from exc
    return aioredis.from_url(dsn, decode_responses=True)


class RedisStorage:
    """Redis 存储聚合：客户端 + 脚本注册 + ping。

    构造后调用 :meth:`ensure_scripts` 预加载 Lua 脚本（``register_script``），
    之后 ``run_script`` 会优先用 EVALSHA（带自动回退到 EVAL）。
    """

    def __init__(self, dsn: str, *, use_fake: bool = False) -> None:
        self._dsn = dsn
        self._client: Any = build_redis(dsn, use_fake=use_fake)
        self._scripts: dict[str, Any] = {}

    @property
    def client(self) -> Any:
        return self._client

    async def ensure_scripts(self) -> None:
        from .scripts import SCRIPTS  # noqa: PLC0415

        for name, source in SCRIPTS.items():
            self._scripts[name] = self._client.register_script(source)

    async def run_script(self, name: str, keys: list[str], args: list[Any]) -> Any:
        """执行已注册的 Lua 脚本（``register_script`` 产出可调用对象）。"""
        script = self._scripts.get(name)
        if script is None:
            raise StorageUnavailableError(f"Lua 脚本未注册：{name}", detail={"component": "redis"})
        return await script(keys=keys, args=[str(a) for a in args])

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    async def set_nx(self, key: str, value: str, *, ttl_s: int) -> bool:
        """``SET key value NX EX ttl``：分布式幂等键。"""
        result = await self._client.set(key, value, nx=True, ex=ttl_s)
        return bool(result)

    async def get(self, key: str) -> str | None:
        value = await self._client.get(key)
        return None if value is None else str(value)

    async def aclose(self) -> None:
        closer: Callable[[], Awaitable[Any]] | None = getattr(self._client, "aclose", None)
        if closer is not None:
            await closer()
            return
        closer = getattr(self._client, "close", None)
        if closer is not None:
            result = closer()
            if hasattr(result, "__await__"):
                await result
