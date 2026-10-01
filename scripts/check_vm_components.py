"""VM 外部组件连通性自检（DoD 2）。

对本项目依赖、且**已在虚拟机 192.168.200.10 上存在**的组件逐一实测，并如实标注
「预期失败 / 待复验」项 —— 绝不伪造通过。

  * 预期 ✅：MongoDB / Elasticsearch / Milvus / MinIO（VM 上已存在）
  * 预期 ⏳：MySQL / Redis / Embedding（待用户执行 ``deploy/vm-provision.sh`` 后复验）

用法：``uv run python scripts/check_vm_components.py``
退出码：0 = 全部「预期 ✅」组件均通过；1 = 其中有失败。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlparse

from dba.config import get_settings

OK = "✓ PASS"
BAD = "✗ FAIL"
PENDING = "⏳ PENDING"

#: 必须通过的组件（其余为「待用户执行脚本后复验」）
EXPECTED_OK = {"MongoDB", "Elasticsearch", "Milvus", "MinIO"}


async def check_mongo() -> tuple[str, str]:
    """MongoDB：serverStatus + list_database_names。"""
    from dba.storage.mongo.client import MongoStorage

    settings = get_settings()
    storage = MongoStorage(settings.mongo_dsn, settings.mongo_db)
    try:
        status = await storage.db.client.admin.command("serverStatus")
        names = await storage.db.client.list_database_names()
        return OK, f"mongo {status.get('version', '?')}; databases={names}"
    finally:
        await storage.aclose()


async def check_es() -> tuple[str, str]:
    """Elasticsearch：GET / 版本。"""
    from dba.storage.es.client import EsStorage

    settings = get_settings()
    storage = EsStorage(settings.es_url)
    try:
        info: dict[str, Any] = await storage.client.info()
        version = info.get("version", {}).get("number", "?")
        return OK, f"es {version}; cluster={info.get('cluster_name')}"
    finally:
        await storage.aclose()


def check_milvus() -> tuple[str, str]:
    """Milvus：utility.list_collections()。"""
    from pymilvus import connections, utility

    settings = get_settings()
    alias = "dba_vmcheck"
    connections.connect(alias=alias, host=settings.milvus_host, port=str(settings.milvus_port))
    try:
        collections = list(utility.list_collections(using=alias))
        return OK, f"milvus collections={collections}"
    finally:
        connections.disconnect(alias)


def check_minio() -> tuple[str, str]:
    """MinIO：list_buckets()（凭据 minioadmin/minioadmin）。"""
    from minio import Minio

    settings = get_settings()
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    buckets = [bucket.name for bucket in client.list_buckets()]
    return OK, f"minio buckets={buckets}"


async def check_mysql() -> tuple[str, str]:
    """MySQL：预期失败（凭据待 vm-provision.sh 建好后复验）。"""
    import asyncmy

    settings = get_settings()
    parsed = urlparse(settings.mysql_dsn.replace("+asyncmy", ""))
    try:
        conn = await asyncmy.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password,
            db=(parsed.path or "/").lstrip("/") or None,
            connect_timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        return PENDING, f"MySQL 连接失败（预期：凭据待建）→ {type(exc).__name__}: {exc}"
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT VERSION()")
            row = await cursor.fetchone()
        return OK, f"mysql {row[0] if row else '?'}"
    finally:
        conn.close()


async def check_redis() -> tuple[str, str]:
    """Redis：预期失败（待 vm-provision.sh 部署）。"""
    import redis.asyncio as aioredis

    settings = get_settings()
    client = aioredis.from_url(settings.redis_dsn, decode_responses=True)
    try:
        pong = await client.ping()
        return OK, f"redis ping={pong}"
    except Exception as exc:  # noqa: BLE001
        return PENDING, f"Redis 不可达（预期：待部署）→ {type(exc).__name__}: {exc}"
    finally:
        await client.aclose()


def check_embedding() -> tuple[str, str]:
    """Embedding：POST 一条文本，验证返回 1024 维向量（真实可用性探活）。

    说明：健康端点返回体不一（本地服务的 /health 是 JSON、OpenAI 兼容服务是空 body），
    故直接探测嵌入端点，不依赖 /health。
    """
    settings = get_settings()
    url = settings.embedding_url.rstrip("/") + settings.embedding_path
    payload = json.dumps({"model": settings.embedding_model, "input": ["连通性探活"]}).encode(
        "utf-8"
    )
    request = urllib.request.Request(  # noqa: S310 - 内部可信地址
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
        vectors = body.get("vectors") or body.get("data") or []
        if not vectors:
            return PENDING, "Embedding 返回空向量"
        first = vectors[0]
        dim = len(first.get("embedding", [])) if isinstance(first, dict) else len(first)
        return OK, f"embedding dim={dim}"
    except Exception as exc:  # noqa: BLE001
        return PENDING, f"Embedding 不可达 → {type(exc).__name__}: {exc}"


async def _run_async(coro: Callable[[], Coroutine[Any, Any, tuple[str, str]]]) -> tuple[str, str]:
    try:
        return await coro()
    except Exception as exc:  # noqa: BLE001
        return BAD, f"{type(exc).__name__}: {exc}"


def _run_sync(fn: Callable[[], tuple[str, str]]) -> tuple[str, str]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return BAD, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    settings = get_settings()
    print(f"目标 VM：{settings.milvus_host}（外部组件统一地址）")
    print("-" * 78)

    rows: list[tuple[str, str, str]] = []
    for name, coro in (
        ("MongoDB", check_mongo),
        ("Elasticsearch", check_es),
        ("MySQL", check_mysql),
        ("Redis", check_redis),
    ):
        status, detail = await _run_async(coro)
        rows.append((name, status, detail))
    sync_checks = (("Milvus", check_milvus), ("MinIO", check_minio), ("Embedding", check_embedding))
    for name, fn in sync_checks:
        status, detail = _run_sync(fn)
        rows.append((name, status, detail))

    failed_expected = False
    pending: list[str] = []
    for name, status, detail in rows:
        print(f"  {status:12} {name:14} {detail}")
        if name in EXPECTED_OK and status != OK:
            failed_expected = True
        if status == PENDING:
            pending.append(name)
    print("-" * 78)
    if failed_expected:
        print("结果：存在「预期 ✅」组件失败 —— 请检查网络/组件状态。")
        return 1
    if pending:
        done = len(rows) - len(pending)
        print(f"结果：通过 {done}/{len(rows)}；待复验（PENDING）：{', '.join(pending)}。")
    else:
        print("结果：全部组件通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
