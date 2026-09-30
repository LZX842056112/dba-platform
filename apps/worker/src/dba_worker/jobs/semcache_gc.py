"""worker 任务：语义缓存 GC（``semantic_cache_entry`` 过期清理）。

对齐《设计方案 v2》§5.6 / §5.4.x 与《实现要点清单》§1.6.6。

为什么需要
----------
``semantic_cache_entry``（Mongo）承载「同 scope 的问答复用」，条目带 ``expire_at``。若不清：
* 集合无限膨胀、命中索引退化；
* 更严重的是——**过期条目仍可能被读到**，把「昨天权限范围内的答案」当成今天的新鲜答案
  （故命中时必须回本集合校验 ``scope_hash``；GC 是第二道防线）。

口径：删除 ``expire_at < now`` 的条目。幂等、可重试。
★ 诚实降级：Mongo 不可用时返回 ``storage_unavailable``，绝不假装清理成功。
"""

from __future__ import annotations

import logging
from typing import Any

from ._common import naive_now

__all__ = ["run_semcache_gc"]

logger = logging.getLogger("dba.worker.jobs.semcache_gc")


async def run_semcache_gc(container: Any) -> dict[str, Any]:
    """清理过期的语义缓存条目。"""
    mongo_repos = container.get("mongo_repos")
    if mongo_repos is None:
        logger.warning("语义缓存 GC 跳过：MongoDB 未装配")
        return {"ok": False, "reason": "storage_unavailable"}

    now = naive_now()
    try:
        deleted = int(await mongo_repos.semantic_cache_entry.delete_expired(now) or 0)
    except Exception as exc:  # noqa: BLE001 - GC 失败不抛（由监控感知）
        logger.warning("语义缓存 GC 失败：%s", exc)
        return {"ok": False, "reason": "gc_failed", "error": str(exc)}

    logger.info("语义缓存 GC 完成 deleted=%d at=%s", deleted, now.isoformat())
    return {"ok": True, "deleted": deleted, "at": now.isoformat()}
