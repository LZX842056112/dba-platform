"""B2/B3 测试夹具。

* 真实 MySQL 测试通过环境变量 ``DBA_TEST_MYSQL_DSN`` 开关（未设置/连不上则 **skip**，
  绝不假装通过）；CI 里由 ``scripts/bootstrap.sh`` 起库后注入该变量。
* Redis 侧统一用 ``fakeredis[lua]``（基于 lupa 的**真实 Lua**），无需真实 Redis。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

MYSQL_DSN_ENV = "DBA_TEST_MYSQL_DSN"


def mysql_dsn_or_skip() -> str:
    dsn = os.environ.get(MYSQL_DSN_ENV)
    if not dsn:
        pytest.skip(f"未设置 {MYSQL_DSN_ENV}：跳过真实 MySQL 测试")
    return dsn


@pytest.fixture
def mysql_dsn() -> str:
    return mysql_dsn_or_skip()


@pytest.fixture
def fake_redis_uri() -> Iterator[str]:
    yield "redis://fake/0"


@pytest.fixture
async def redis_storage() -> AsyncIterator[object]:
    """已注册 Lua 脚本的 fakeredis 存储。"""
    from dba.storage.redis.client import RedisStorage

    storage = RedisStorage("redis://fake/0", use_fake=True)
    await storage.ensure_scripts()
    try:
        yield storage
    finally:
        await storage.aclose()
