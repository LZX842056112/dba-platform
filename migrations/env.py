"""Alembic 环境（MySQL，异步驱动）。

对齐《设计文档 v2》§4.7 与《实现要点清单》§1.2.6.15。

* URL 优先取命令行/``config`` 的 ``sqlalchemy.url``，其次取平台配置 ``Settings.mysql_dsn``；
* ``target_metadata`` 在 B2 提供 ``dba.storage.mysql.models`` 后自动接入（此前为 None）；
* 支持 offline（生成 SQL）与 online（真实连接，``asyncmy`` 异步驱动）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Alembic Config 对象
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    """解析数据库 URL：命令行 > 平台配置。"""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    try:
        from dba.config import get_settings  # noqa: PLC0415

        return get_settings().mysql_dsn
    except Exception:  # noqa: BLE001 - 迁移环境与运行环境解耦，读不到就用占位
        return "mysql+asyncmy://dba_user:dba_user_pwd_2026@192.168.200.10:3306/dba"


def _target_metadata() -> object | None:
    """接入 ORM 元数据（B2 提供 models 后自动生效）。"""
    try:
        from dba.storage.mysql.models import Base  # noqa: PLC0415

        return Base.metadata
    except Exception:  # noqa: BLE001
        return None


target_metadata = _target_metadata()


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：异步引擎执行迁移。"""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
