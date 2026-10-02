"""Elasticsearch 客户端封装（elasticsearch[async]，懒加载）。

★ ``ensure_indices`` 做四件事：
  1. 装 ILM 策略 ``dba-retention``（``_ilm/policy``）；
  2. 为每个索引建 **index template**（``dynamic: strict`` + ILM 绑定 + 分片/副本）；
  3. 创建 ``-000001`` 写索引并挂 ``write`` alias；
  4. 校验 alias 存在（后续全部走 alias 写入，不写裸索引名）。
"""

from __future__ import annotations

import logging
from typing import Any

from dba_runtime.errors import StorageUnavailableError

from .indices import ILM_POLICY_NAME, INDEX_SPECS, build_ilm_policy

__all__ = ["EsStorage", "build_es"]

logger = logging.getLogger("dba.storage.es")


def build_es(url: str) -> Any:
    try:
        from elasticsearch import AsyncElasticsearch  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StorageUnavailableError(
            "Elasticsearch 需要安装 `dba[es]`（elasticsearch[async]）",
            detail={"component": "elasticsearch"},
        ) from exc
    return AsyncElasticsearch(url, request_timeout=10)


class EsStorage:
    """ES 存储聚合：客户端 + 索引/ILM 初始化 + 泛化检索。"""

    def __init__(self, url: str, *, prefix: str = "dba") -> None:
        self._client: Any = build_es(url)
        self._prefix = prefix

    @property
    def client(self) -> Any:
        return self._client

    def alias(self, suffix: str) -> str:
        return f"{self._prefix}-{suffix}"

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    async def ensure_indices(self) -> None:
        # 1) ILM 策略
        from elasticsearch import NotFoundError  # noqa: PLC0415

        body = build_ilm_policy(INDEX_SPECS[0])
        try:
            await self._client.ilm.put_lifecycle(name=ILM_POLICY_NAME, policy=body["policy"])
        except Exception:  # noqa: BLE001 - 策略已存在或无权限时降级为告警
            logger.warning("写入 ILM 策略失败（可能已存在）", exc_info=True)

        for spec in INDEX_SPECS:
            template_name = f"{self._prefix}-{spec.suffix}"
            template = {
                "index_patterns": [f"{self._prefix}-{spec.suffix}-*"],
                "template": {
                    "settings": {
                        "number_of_shards": 3,
                        "number_of_replicas": 1,
                        "index.lifecycle.name": ILM_POLICY_NAME,
                        "index.lifecycle.rollover_alias": template_name,
                    },
                    "mappings": {"dynamic": "strict", "properties": spec.properties},
                },
            }
            await self._client.indices.put_index_template(name=template_name, **template)
            bootstrap = f"{self._prefix}-{spec.suffix}-000001"
            if not await self._client.indices.exists(index=bootstrap):
                await self._client.indices.create(
                    index=bootstrap,
                    aliases={template_name: {"is_write_index": True}},
                )
            if spec.suffix == "run-event":
                # 更新已有 concrete index；仅更新 template 不会修复当前 write index。
                existing = await self._client.indices.get_alias(name=template_name)
                for index_name in existing:
                    await self._client.indices.put_mapping(
                        index=index_name, properties=spec.properties
                    )
        try:
            await self._client.indices.exists_alias(name=f"{self._prefix}-run-event")
        except NotFoundError:  # pragma: no cover
            logger.warning("alias 校验失败，请检查 ES 权限")

    async def index(self, alias: str, doc: dict[str, Any]) -> None:
        await self._client.index(index=alias, document=doc)

    async def search(self, alias: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._client.search(index=alias, **body)
        hits = result.get("hits", {}).get("hits", [])
        return [{"_id": h.get("_id"), **h.get("_source", {})} for h in hits]

    async def aclose(self) -> None:
        await self._client.close()
