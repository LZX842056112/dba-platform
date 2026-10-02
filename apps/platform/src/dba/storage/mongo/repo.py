"""MongoDB Repository 实现（L5）。

对齐《设计文档 v2》§5.4 与《实现要点清单》§6.14（U16）。

★ U16（``seq`` 原子分配，照抄 v1 会怎样错）
-----------------------------------------
v1 写聊天消息前先 ``count_documents`` 得到 ``seq``，再 INSERT。并发下两条消息读到同一个
``count``，同时插入 → 撞 ``(session_id, seq)`` 唯一键，其中一条报错。
v2 改为在 ``chat_session`` 上用 ``find_one_and_update({...}, {"$inc": {"message_count": 1}})
`` **原子**分配 ``seq``（``return_document=AFTER`` 取回新值），并发不撞键。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from dba_runtime.errors import StorageUnavailableError

from .client import MongoStorage

__all__ = [
    "ChatSessionRepo",
    "ChatMessageRepo",
    "DashboardSpecRepo",
    "RunDocRepo",
    "SkillDefRepo",
    "AnomalyReportRepo",
    "FinopsRecommendationRepo",
    "SemanticCacheEntryRepo",
    "MongoRepositories",
]

Row = dict[str, Any]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _clean(doc: Row | None) -> Row | None:
    if doc is None:
        return None
    out = dict(doc)
    if "_id" in out:
        out["_id"] = str(out["_id"])
    return out


def _clean_many(docs: list[Row]) -> list[Row]:
    return [_clean(d) or {} for d in docs]


class _Repo:
    def __init__(self, storage: MongoStorage) -> None:
        self._storage = storage

    def _coll(self, name: str) -> Any:
        return self._storage.collection(name)

    async def _run(self, coro: Any) -> Any:
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError(
                f"MongoDB 操作失败：{exc}", detail={"component": "mongodb"}
            ) from exc


class ChatSessionRepo(_Repo):
    async def create(self, doc: Row) -> Row:
        now = _now()
        payload = {"message_count": 0, "created_at": now, "updated_at": now, **doc}
        await self._run(self._coll("chat_session").insert_one(payload))
        return _clean(payload) or {}

    async def get(self, session_id: str) -> Row | None:
        return _clean(
            await self._run(self._coll("chat_session").find_one({"session_id": session_id}))
        )

    async def soft_delete(self, session_id: str, user_id: int) -> bool:
        """按会话归属标记删除，保留会话消息供审计。"""
        result = await self._run(
            self._coll("chat_session").update_one(
                {"session_id": session_id, "user_id": user_id},
                {"$set": {"deleted": True, "updated_at": _now()}},
            )
        )
        return bool(result.matched_count)

    async def list_by_user(self, user_id: int, *, limit: int = 20) -> list[Row]:
        cursor = (
            self._coll("chat_session")
            .find({"user_id": user_id, "deleted": {"$ne": True}})
            .sort("updated_at", -1)
            .limit(limit)
        )
        return _clean_many(await self._run(cursor.to_list(length=limit)))

    async def next_message_seq(self, session_id: str) -> int:
        """★ U16：原子分配消息序号（``$inc``）。"""
        from pymongo import ReturnDocument  # noqa: PLC0415

        result = await self._run(
            self._coll("chat_session").find_one_and_update(
                {"session_id": session_id},
                {"$inc": {"message_count": 1}, "$set": {"updated_at": _now()}},
                return_document=ReturnDocument.AFTER,
                upsert=True,
            )
        )
        if result is None:  # pragma: no cover - upsert=True 时不应发生
            raise StorageUnavailableError("分配 message seq 失败", detail={"component": "mongodb"})
        return int(result.get("message_count", 1))


class ChatMessageRepo(_Repo):
    async def append(self, doc: Row) -> Row:
        payload = {"created_at": _now(), **doc}
        await self._run(self._coll("chat_message").insert_one(payload))
        return _clean(payload) or {}

    async def list_by_session(self, session_id: str, *, limit: int = 50) -> list[Row]:
        cursor = (
            self._coll("chat_message").find({"session_id": session_id}).sort("seq", 1).limit(limit)
        )
        return _clean_many(await self._run(cursor.to_list(length=limit)))


class DashboardSpecRepo(_Repo):
    async def save_new_version(self, doc: Row) -> int:
        dashboard_id = str(doc["dashboard_id"])
        latest = await self._coll("dashboard_spec").find_one(
            {"dashboard_id": dashboard_id}, sort=[("version", -1)]
        )
        version = int(latest["version"]) + 1 if latest else 1
        payload = {"version": version, "created_at": _now(), **doc}
        await self._run(self._coll("dashboard_spec").insert_one(payload))
        return version

    async def latest(self, dashboard_id: str) -> Row | None:
        return _clean(
            await self._run(
                self._coll("dashboard_spec").find_one(
                    {"dashboard_id": dashboard_id}, sort=[("version", -1)]
                )
            )
        )

    async def versions(self, dashboard_id: str) -> list[Row]:
        cursor = (
            self._coll("dashboard_spec").find({"dashboard_id": dashboard_id}).sort("version", 1)
        )
        return _clean_many(await self._run(cursor.to_list(length=100)))

    async def mark_published(self, dashboard_id: str, url: str) -> None:
        await self._run(
            self._coll("dashboard_spec").update_many(
                {"dashboard_id": dashboard_id},
                {"$set": {"published_url": url, "published_at": _now()}},
            )
        )


class RunDocRepo(_Repo):
    """span 树的权威文档（owner: capabilities.telemetry）。"""

    async def upsert(self, doc: Row) -> None:
        await self._run(
            self._coll("run_doc").update_one(
                {"trace_id": doc["trace_id"]}, {"$set": doc}, upsert=True
            )
        )

    async def get(self, trace_id: str) -> Row | None:
        return _clean(await self._run(self._coll("run_doc").find_one({"trace_id": trace_id})))


class SkillDefRepo(_Repo):
    async def latest(self, skill_key: str) -> Row | None:
        return _clean(
            await self._run(
                self._coll("skill_def").find_one({"skill_key": skill_key}, sort=[("version", -1)])
            )
        )

    async def save_version(self, doc: Row) -> None:
        await self._run(self._coll("skill_def").insert_one({"created_at": _now(), **doc}))


class AnomalyReportRepo(_Repo):
    """★ U14：归因的**权威源**（MySQL ``alert_event.attribution_json`` 只存展示摘要）。"""

    async def save(self, doc: Row) -> None:
        await self._run(
            self._coll("anomaly_report").update_one(
                {"alert_id": doc["alert_id"]}, {"$set": doc}, upsert=True
            )
        )

    async def get_by_alert(self, alert_id: int) -> Row | None:
        return _clean(
            await self._run(self._coll("anomaly_report").find_one({"alert_id": alert_id}))
        )


class FinopsRecommendationRepo(_Repo):
    async def save(self, doc: Row) -> None:
        await self._run(
            self._coll("finops_recommendation").update_one(
                {"reco_id": doc["reco_id"]}, {"$set": doc}, upsert=True
            )
        )

    async def list(self, flt: Row) -> list[Row]:
        query: Row = {}
        if flt.get("status"):
            query["status"] = flt["status"]
        if flt.get("biz_line_id") is not None:
            query["biz_line_id"] = flt["biz_line_id"]
        if flt.get("scope_type"):
            query["scope_type"] = flt["scope_type"]
        if flt.get("scope_id") is not None:
            query["scope_id"] = flt["scope_id"]
        limit = int(flt.get("limit", 50))
        cursor = self._coll("finops_recommendation").find(query).sort("created_at", -1).limit(limit)
        return _clean_many(await self._run(cursor.to_list(length=limit)))

    async def set_status(self, reco_id: str, status: str) -> None:
        await self._run(
            self._coll("finops_recommendation").update_one(
                {"reco_id": reco_id}, {"$set": {"status": status, "updated_at": _now()}}
            )
        )


class SemanticCacheEntryRepo(_Repo):
    """★ 语义缓存命中必须回本集合校验 ``scope_hash``（防越权复用其他权限范围的答案）。"""

    async def get(self, query_hash: str, biz_line_id: int, scope_hash: str) -> Row | None:
        return _clean(
            await self._run(
                self._coll("semantic_cache_entry").find_one(
                    {"query_hash": query_hash, "biz_line_id": biz_line_id, "scope_hash": scope_hash}
                )
            )
        )

    async def upsert(self, doc: Row) -> None:
        key = {
            "query_hash": doc["query_hash"],
            "biz_line_id": doc["biz_line_id"],
            "scope_hash": doc["scope_hash"],
        }
        await self._run(
            self._coll("semantic_cache_entry").update_one(
                key, {"$set": {"created_at": _now(), **doc}}, upsert=True
            )
        )

    async def delete_expired(self, now: dt.datetime) -> int:
        result = await self._run(
            self._coll("semantic_cache_entry").delete_many({"expire_at": {"$lt": now}})
        )
        return int(getattr(result, "deleted_count", 0))


class MongoRepositories:
    """一次性构造全部 Mongo Repo。"""

    def __init__(self, storage: MongoStorage) -> None:
        self.chat_session = ChatSessionRepo(storage)
        self.chat_message = ChatMessageRepo(storage)
        self.dashboard_spec = DashboardSpecRepo(storage)
        self.run_doc = RunDocRepo(storage)
        self.skill_def = SkillDefRepo(storage)
        self.anomaly_report = AnomalyReportRepo(storage)
        self.finops_recommendation = FinopsRecommendationRepo(storage)
        self.semantic_cache_entry = SemanticCacheEntryRepo(storage)
