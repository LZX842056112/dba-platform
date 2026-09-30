"""Alembic 迁移的执行封装（供 ``dba migrate`` / ``dba.storage.mysql.migrations_runner`` 复用）。

对齐《设计文档 v2》§4.7 与《实现要点清单》§1.2.6.15。

★ 幂等性从何而来（DoD 2 要求 ``dba migrate`` 连续跑两次结果一致）：
  Alembic 自带 ``alembic_version`` 表记录当前 revision；``upgrade head`` 在已到 head 时
  是 **no-op**（不会重复执行 ``op.create_table``）。因此幂等不是靠本模块额外做判断，
  而是靠「迁移脚本本身可重入 / 版本表」这一标准机制。本模块只负责把 DSN 与脚本目录
  正确接上，并把异常翻译成清晰的日志。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dba_runtime.errors import StorageUnavailableError

__all__ = ["run_upgrade", "current_revision", "find_migrations_dir"]

logger = logging.getLogger("dba.storage.mysql.migrate")


def find_migrations_dir() -> Path:
    """定位 workspace 根下的 ``migrations/`` 目录。

    ``apps/platform/src/dba/storage/mysql/migrations_runner.py`` → 上溯 6 层到 workspace 根。
    """
    return Path(__file__).resolve().parents[6] / "migrations"


def _build_config(dsn: str, migrations_dir: Path) -> Any:
    try:
        from alembic.config import Config  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "迁移需要安装 alembic（`dba[mysql]`）", detail={"component": "mysql"}
        ) from exc
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def run_upgrade(dsn: str, revision: str = "head", *, migrations_dir: Path | None = None) -> str:
    """执行 Alembic 升级（幂等）。返回目标 revision。"""
    from alembic import command  # noqa: PLC0415

    directory = migrations_dir or find_migrations_dir()
    if not directory.exists():  # pragma: no cover - 目录缺失属部署错误
        raise StorageUnavailableError(f"未找到迁移目录：{directory}", detail={"component": "mysql"})
    cfg = _build_config(dsn, directory)
    command.upgrade(cfg, revision)
    logger.info("migrate 完成：%s → %s", dsn.split("@")[-1], revision)
    return revision


def current_revision(dsn: str, *, migrations_dir: Path | None = None) -> str | None:
    """读取当前库已应用的 revision（无版本表则返回 ``None``）。"""
    import sqlalchemy as sa  # noqa: PLC0415

    url = dsn.replace("mysql+asyncmy://", "mysql+pymysql://")
    try:
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    except ModuleNotFoundError:  # pragma: no cover - 无同步驱动时退化为异步查询
        return None
    try:
        with engine.connect() as conn:
            if not sa.inspect(conn).has_table("alembic_version"):
                return None
            return str(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar())
    finally:
        engine.dispose()
