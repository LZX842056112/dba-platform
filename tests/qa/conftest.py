"""QA 独立测试目录的夹具（镜像 apps/platform/tests/conftest.py 的真实 MySQL 开关）。"""

from __future__ import annotations

import os

import pytest

MYSQL_DSN_ENV = "DBA_TEST_MYSQL_DSN"


@pytest.fixture
def mysql_dsn() -> str:
    dsn = os.environ.get(MYSQL_DSN_ENV)
    if not dsn:
        pytest.skip(f"未设置 {MYSQL_DSN_ENV}：跳过真实 MySQL 测试")
    return dsn
