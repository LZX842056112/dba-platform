"""MongoDB 客户端封装（motor 异步驱动，懒加载）。

★ 为什么懒加载：``motor`` 是可选 extra（``dba[mongo]``）；应用在「无 Mongo」时不
应因 import 失败而崩溃，而应在真正访问时报 ``STORAGE_UNAVAILABLE``。
"""

from __future__ import annotations

import logging
from typing import Any

from dba_runtime.errors import StorageUnavailableError

__all__ = ["MongoStorage", "build_mongo"]

logger = logging.getLogger("dba.storage.mongo")

#: 8 个集合名（§5.4）
COLLECTIONS: tuple[str, ...] = (
    "chat_session",
    "chat_message",
    "dashboard_spec",
    "run_doc",
    "skill_def",
    "anomaly_report",
    "finops_recommendation",
    "semantic_cache_entry",
)


def build_mongo(dsn: str) -> Any:
    """构造 motor AsyncIOMotorClient。"""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "MongoDB 需要安装 `dba[mongo]`（motor）", detail={"component": "mongodb"}
        ) from exc
    return AsyncIOMotorClient(dsn, serverSelectionTimeoutMS=3000)


class MongoStorage:
    """Mongo 存储聚合：客户端 + 数据库句柄 + ping / 索引初始化。"""

    def __init__(self, dsn: str, db_name: str = "dba") -> None:
        self._client: Any = build_mongo(dsn)
        self._db: Any = self._client[db_name]
        self._db_name = db_name

    @property
    def db(self) -> Any:
        return self._db

    def collection(self, name: str) -> Any:
        return self._db[name]

    async def ping(self) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def ensure_indexes(self) -> None:
        """建立集合与索引（``dba bootstrap-storage`` 调用）。

        * ``chat_message``：``(session_id, seq)`` 唯一 —— 配合 U16 的 ``$inc`` 原子 seq；
        * ``semantic_cache_entry``：``(query_hash, biz_line_id, scope_hash)`` 唯一
          —— 命中回校验 scope_hash；
        * ``run_doc``：``trace_id`` 唯一；
        * ``finops_recommendation``：``created_at`` 降序。
        """
        await self.collection("chat_session").create_index([("session_id", 1)], unique=True)
        await self.collection("chat_session").create_index([("user_id", 1), ("updated_at", -1)])
        await self.collection("chat_message").create_index(
            [("session_id", 1), ("seq", 1)], unique=True
        )
        await self.collection("dashboard_spec").create_index(
            [("dashboard_id", 1), ("version", -1)], unique=True
        )
        await self.collection("run_doc").create_index([("trace_id", 1)], unique=True)
        await self.collection("skill_def").create_index(
            [("skill_key", 1), ("version", -1)], unique=True
        )
        await self.collection("anomaly_report").create_index([("alert_id", 1)], unique=True)
        await self.collection("finops_recommendation").create_index([("created_at", -1)])
        await self.collection("semantic_cache_entry").create_index(
            [("query_hash", 1), ("biz_line_id", 1), ("scope_hash", 1)], unique=True
        )
        await self.collection("semantic_cache_entry").create_index([("expire_at", 1)])

    async def aclose(self) -> None:
        self._client.close()
