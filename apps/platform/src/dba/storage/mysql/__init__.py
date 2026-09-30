"""MySQL 存储适配（SQLAlchemy 2.x async + asyncmy）。

文件：
* ``engine.py``   —— 读写引擎 + **独立只读连接池**
* ``models.py``   —— 21 张表 ORM 模型（严格对齐 §5.2 DDL）
* ``repo.py``     —— 各表 Repository 实现（owner 独占写）
* ``migrations_runner.py`` —— Alembic 运行封装（``dba migrate``）

★ 本包顶层会 import ``sqlalchemy``（属 ``dba[mysql]`` extra）——
  只有在**确实要用 MySQL** 时才 import 本包，应用启动不 import 它即不受影响。
"""

from __future__ import annotations

from .engine import MySqlStorage, ReadOnlyPool, build_engine
from .migrations_runner import current_revision, find_migrations_dir, run_upgrade
from .repo import MysqlRepositories

__all__ = [
    "MySqlStorage",
    "MysqlRepositories",
    "ReadOnlyPool",
    "build_engine",
    "current_revision",
    "find_migrations_dir",
    "run_upgrade",
]
