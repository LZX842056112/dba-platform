"""MySQL 引擎与**独立只读连接池**。

对齐《设计文档 v2》§3.1 / §6.3.4 与《实现要点清单》§5.2、红线 6。

★ 为什么需要独立只读池（照抄 v1「一个池跑全部」会怎样错）：
  红线 6 规定「全平台唯一真实 SQL 执行入口是 ``QueryExecutor``，且必须走**只读账号**」。
  若与业务写入共用一个池，就无法在连接层强制只读，护栏只能靠 SQL 文本判断——
  一旦护栏有绕过（注释、UNION、CTE），越权读就会真正打到库上。
  只读池做三件事：
    ① 独立账号（``DBA_MYSQL_RO_DSN``，生产用只读账号）；
    ② 每条连接 ``SET SESSION TRANSACTION READ ONLY``（数据库层硬保证）；
    ③ 关闭多语句（不启用 ``CLIENT.MULTI_STATEMENTS``），杜绝 ``; DROP`` 类注入；
    ④ **不共享 ORM 会话**——只读池只做 Core 执行，返回行字典。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from dba_runtime.errors import StorageUnavailableError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger("dba.storage.mysql")

#: 数据库层强制只读（部分云 MySQL 不支持时会在连接初始化报错，由 ``read_only_enforced`` 记录）
_READ_ONLY_INIT = (
    "SET SESSION TRANSACTION READ ONLY",
    "SET SESSION time_zone = '+00:00'",
)


def _require_sqlalchemy() -> Any:
    try:
        import sqlalchemy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "MySQL 存储需要安装 `dba[mysql]`（sqlalchemy[asyncio] / asyncmy / alembic）",
            detail={"component": "mysql"},
        ) from exc
    return sqlalchemy


def build_engine(
    dsn: str, *, pool_size: int = 10, max_overflow: int = 20, echo: bool = False
) -> AsyncEngine:
    """创建读写引擎（业务写入用）。"""
    _require_sqlalchemy()
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    return create_async_engine(
        dsn,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=echo,
        connect_args={"charset": "utf8mb4"},
    )


class ReadOnlyPool:
    """★ 独立只读连接池（红线 6）。

    用法::

        pool = ReadOnlyPool(settings.mysql_ro_dsn)
        async with pool.acquire() as conn:
            result = await conn.exec_driver_sql(sql)
            rows = [dict(r._mapping) for r in result.fetchall()]
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 5,
        enforce_read_only: bool = True,
        timeout_s: float = 30.0,
    ) -> None:
        self._dsn = dsn
        self._enforce = enforce_read_only
        self._timeout_s = timeout_s
        self.read_only_enforced = False
        _require_sqlalchemy()
        from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

        # ★ 不传 client_flag=CLIENT.MULTI_STATEMENTS：默认即禁用多语句
        self._engine = create_async_engine(
            dsn,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
            connect_args={"charset": "utf8mb4", "autocommit": True},
        )

    async def _init_connection(self, conn: AsyncConnection) -> None:
        if not self._enforce:
            return
        try:
            for stmt in _READ_ONLY_INIT:
                await conn.exec_driver_sql(stmt)
            self.read_only_enforced = True
        except Exception:  # noqa: BLE001 - 云 MySQL 可能不支持，降级为「账号级只读」
            self.read_only_enforced = False
            logger.warning("只读会话设置失败，降级为账号级只读（请确保 RO 账号本身无写权限）")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[AsyncConnection]:
        """取一条只读连接（含每条连接的只读会话初始化）。"""
        try:
            async with self._engine.connect() as conn:
                # 执行层再兜一道：语句超时，防止长查询拖垮只读池
                await conn.exec_driver_sql(
                    f"SET SESSION max_execution_time = {int(self._timeout_s * 1000)}"
                )
                await self._init_connection(conn)
                yield conn
        except StorageUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError(
                f"MySQL 只读池不可用：{exc}", detail={"component": "mysql"}
            ) from exc

    async def aclose(self) -> None:
        await self._engine.dispose()


class MySqlStorage:
    """MySQL 存储聚合：读写引擎 + 只读池（DI 容器持有它）。"""

    def __init__(self, *, rw_dsn: str, ro_dsn: str, echo: bool = False) -> None:
        self.rw_engine: AsyncEngine = build_engine(rw_dsn, echo=echo)
        self.read_only = ReadOnlyPool(ro_dsn)

    async def ping(self) -> bool:
        try:
            async with self.read_only.acquire() as conn:
                await conn.exec_driver_sql("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def aclose(self) -> None:
        await self.read_only.aclose()
        await self.rw_engine.dispose()
