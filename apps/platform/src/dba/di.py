"""轻量依赖注入容器 + **平台装配**（手写，不引框架）。

对齐《设计文档 v2》§4.2 与《实现要点清单》§1.2.6.3。

设计取舍：
* 容器只做「按 key 取实现 + 单例缓存 + 测试覆盖」三件事，不引入 DI 框架；
* **分层装配**：内核定义 Protocol，平台侧在此把 Protocol 绑到实现；
* **B2/B3 已接线**：本模块把 ``storage/*``（L5）与 ``capabilities/*``（L4）装配进容器；
  任何**可选驱动缺失**（未装 ``dba[mysql]`` 等）都不会让应用启动失败——
  对应组件降级为占位实现，并在 ``/ready`` 里如实反映（绝不假装可用）。
* 占位实现一律显式命名 ``*Placeholder``，绝不冒充已完成的实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dba_runtime.telemetry import (
    LLMCallRecord,
    MeteringService,
    NormalizedCost,
    SpanRecord,
    ToolCallRecord,
)

from .config import Settings

logger = logging.getLogger("dba.di")

Factory = Callable[[], Any]
Key = str


# ─────────────────────────────────────────────────────────────────────
# 占位实现（可选驱动缺失 / 测试环境时降级；绝不冒充真实实现）
# ─────────────────────────────────────────────────────────────────────
class PlaceholderMeteringService(MeteringService):
    """占位计量服务：只写内存/日志，不落库（仅当 MySQL 不可用时启用）。"""

    def __init__(self) -> None:
        self.llm_calls: list[LLMCallRecord] = []
        self.tool_calls: list[ToolCallRecord] = []
        self.spans: list[SpanRecord] = []
        self.dlq: list[tuple[str, dict[str, Any]]] = []

    async def record_llm(self, rec: LLMCallRecord) -> None:
        self.llm_calls.append(rec)

    async def record_tool(self, rec: ToolCallRecord) -> None:
        self.tool_calls.append(rec)

    async def record_span(self, rec: SpanRecord) -> None:
        self.spans.append(rec)

    async def record_dlq(self, kind: str, rec: dict[str, Any]) -> None:
        logger.warning("telemetry DLQ kind=%s（占位实现）", kind)
        self.dlq.append((kind, rec))


class PlaceholderCostNormalizer:
    """占位成本归一化：恒返回 unknown（不静默按 0 计）。

    仅当价格表不可用时启用；真实实现为 ``capabilities.cost.CostNormalizer``。
    """

    def normalize(self, rec: LLMCallRecord) -> NormalizedCost:
        _ = rec
        return NormalizedCost.unknown()


class TcpHealthProbe:
    """TCP 可达性探测（无 DB 驱动时的兜底）。"""

    def __init__(self, targets: dict[str, tuple[str, int]], *, timeout_s: float = 0.4) -> None:
        self._targets = targets
        self._timeout_s = timeout_s

    async def probe(self) -> dict[str, dict[str, Any]]:
        names = list(self._targets)
        results = await asyncio.gather(*(self._probe_one(n, self._targets[n]) for n in names))
        return dict(zip(names, results, strict=True))

    async def _probe_one(self, name: str, target: tuple[str, int]) -> dict[str, Any]:
        host, port = target
        loop_ms = asyncio.get_running_loop().time
        started = loop_ms()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._timeout_s
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭异常不影响可达性结论
                pass
            _ = reader
            return {"ok": True, "latency_ms": int((loop_ms() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {target[0]}:{target[1]}"}


class InMemoryProgressWriter:
    """``run:progress`` 占位（Redis 不可用时启用；仅进程内，无法跨副本回放）。"""

    MAX = 200

    def __init__(self) -> None:
        self._store: dict[str, list[dict[str, Any]]] = {}

    async def write(self, trace_id: str, envelope: dict[str, Any]) -> None:
        bucket = self._store.setdefault(trace_id, [])
        bucket.append(envelope)
        if len(bucket) > self.MAX:
            del bucket[: len(bucket) - self.MAX]

    async def replay(self, trace_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        return [e for e in self._store.get(trace_id, []) if int(e.get("seq", 0)) > after_seq]


class InMemoryAuditSink:
    """审计落点占位（内存）。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class DemoLLMClient:
    """演示 / 离线用确定性 LLM（无 API Key 或测试环境启用）。

    ★ 诚实标注：这不是真实模型，只是「让主链路可端到端跑通」的确定性替身；
      生产必须配置真实模型（``DBA_LLM_API_KEY``）；否则成本曲线与结论均无业务意义。
    按 system prompt 关键词分派：含「SQL」→ 生成 SQL；含「可视化/大屏」→ 大屏 JSON；
    否则 → 解说文本。
    """

    provider = "demo"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    @staticmethod
    def _task(messages: list[dict[str, Any]]) -> str:
        system = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        if "SQL" in system or "sql" in system:
            return "sql"
        if "可视化" in system or "大屏" in system or "布局" in system:
            return "visual"
        return "narrator"

    def _text(self, messages: list[dict[str, Any]]) -> str:
        task = self._task(messages)
        if task == "sql":
            # ★ 多查询信封：q1 为主查询（保 SQL 面板与 e2e 断言），q2~q5 供其它面板取数。
            #   全部单语句只读、结果 ≤500 行（否则会走 s3，而前端暂不拉 s3）。
            return json.dumps(
                {"sql": self._DEMO_QUERIES[0]["sql"], "queries": list(self._DEMO_QUERIES)},
                ensure_ascii=False,
            )
        if task == "visual":
            return json.dumps({"panels": self._demo_panels()}, ensure_ascii=False)
        return "（演示结论）近 30 天 GMV 呈上升趋势；本项目 GMV 为支付口径、不含税。"

    #: 演示模板查询（ref 与面板 `dataset.ref` 对应）
    _DEMO_QUERIES: tuple[dict[str, str], ...] = (
        {
            "ref": "q1",
            "sql": (
                "SELECT dt, SUM(gmv_ex_tax) AS gmv FROM fact_sales "
                "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
                "GROUP BY dt ORDER BY dt LIMIT 500"
            ),
        },
        {
            "ref": "q2",
            "sql": (
                "SELECT province_name AS province, SUM(gmv_ex_tax) AS gmv, "
                "MAX(lat) AS lat, MAX(lon) AS lon FROM fact_sales "
                "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
                "GROUP BY province_name ORDER BY gmv DESC LIMIT 500"
            ),
        },
        {
            "ref": "q3",
            "sql": (
                "SELECT category_name AS category, SUM(gmv_ex_tax) AS gmv, "
                "SUM(order_cnt) AS orders FROM fact_sales "
                "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
                "GROUP BY category_name ORDER BY gmv DESC LIMIT 500"
            ),
        },
        {
            "ref": "q4",
            "sql": (
                "SELECT dt, channel, SUM(gmv_ex_tax) AS gmv FROM fact_sales "
                "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
                "GROUP BY dt, channel ORDER BY dt LIMIT 500"
            ),
        },
        {
            "ref": "q5",
            "sql": (
                "SELECT SUM(gmv_ex_tax) AS gmv, SUM(order_cnt) AS orders, "
                "SUM(profit_ex_tax) AS profit, SUM(uv) AS uv, "
                "ROUND(SUM(profit_ex_tax) / NULLIF(SUM(gmv_ex_tax), 0) * 100, 2) "
                "AS gross_margin FROM fact_sales "
                "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
            ),
        },
    )

    @staticmethod
    def _demo_panels() -> list[dict[str, Any]]:
        """演示驾驶舱：4 个翻牌 KPI + 地图 + 环形占比 + 仪表盘 + 排行 + 堆叠柱 + 趋势。"""
        return [
            {
                "panel_id": "kpi_gmv",
                "kind": "metric_card",
                "title": "GMV 总额",
                "subtitle": "近 30 天 · 不含税",
                "dataset": {"ref": "q5"},
                "encoding": {"y": [{"field": "gmv", "agg": "sum"}]},
                "style": {
                    "variant": "flip",
                    "unit": "元",
                    "trend": "up",
                    "trendValue": 12.4,
                },
            },
            {
                "panel_id": "kpi_orders",
                "kind": "metric_card",
                "title": "订单量",
                "subtitle": "近 30 天",
                "dataset": {"ref": "q5"},
                "encoding": {"y": [{"field": "orders", "agg": "sum"}]},
                "style": {"variant": "flip", "unit": "笔", "trend": "up", "trendValue": 6.8},
            },
            {
                "panel_id": "kpi_profit",
                "kind": "metric_card",
                "title": "毛利额",
                "subtitle": "近 30 天",
                "dataset": {"ref": "q5"},
                "encoding": {"y": [{"field": "profit", "agg": "sum"}]},
                "style": {"variant": "flip", "unit": "元", "trend": "up", "trendValue": 3.2},
            },
            {
                "panel_id": "kpi_uv",
                "kind": "metric_card",
                "title": "访客数",
                "subtitle": "近 30 天",
                "dataset": {"ref": "q5"},
                "encoding": {"y": [{"field": "uv", "agg": "sum"}]},
                "style": {"variant": "flip", "unit": "人", "trend": "down", "trendValue": 1.5},
            },
            {
                "panel_id": "map_province",
                "kind": "chart",
                "title": "省份销售分布",
                "subtitle": "近 30 天 GMV",
                "dataset": {"ref": "q2"},
                "chart": {"type": "map", "region": "china"},
                "encoding": {
                    "x": {"field": "province", "type": "string"},
                    "y": [{"field": "gmv", "agg": "sum"}],
                    "lon": {"field": "lon"},
                    "lat": {"field": "lat"},
                },
                "style": {"mapScatter": True, "mapZoom": 1},
                "w": 8,
                "h": 16,
            },
            {
                "panel_id": "pie_category",
                "kind": "chart",
                "title": "品类结构",
                "subtitle": "GMV 占比",
                "dataset": {"ref": "q3"},
                "chart": {"type": "pie"},
                "encoding": {
                    "x": {"field": "category", "type": "string"},
                    "y": [{"field": "gmv", "agg": "sum"}],
                },
                "style": {"pieVariant": "donut", "sort": "desc", "legend": "bottom"},
                "w": 4,
                "h": 8,
            },
            {
                "panel_id": "gauge_margin",
                "kind": "chart",
                "title": "毛利率",
                "subtitle": "近 30 天",
                "dataset": {"ref": "q5"},
                "chart": {"type": "gauge"},
                "encoding": {"y": [{"field": "gross_margin", "agg": "none"}]},
                "style": {"gaugeMax": 30},
                "w": 4,
                "h": 8,
            },
            {
                "panel_id": "rank_province",
                "kind": "ranking",
                "title": "省份销售排行",
                "dataset": {"ref": "q2"},
                "encoding": {
                    "x": {"field": "province", "type": "string"},
                    "y": [{"field": "gmv", "agg": "sum"}],
                },
                "style": {"sort": "desc", "topN": 8},
                "w": 4,
                "h": 12,
            },
            {
                "panel_id": "bar_channel",
                "kind": "chart",
                "title": "渠道销售堆叠",
                "subtitle": "按日",
                "dataset": {"ref": "q4"},
                "chart": {"type": "bar"},
                "encoding": {
                    "x": {"field": "dt", "type": "time"},
                    "y": [{"field": "gmv", "agg": "sum"}],
                    "series": {"field": "channel"},
                },
                "style": {"stack": True, "legend": "top"},
                "w": 4,
                "h": 12,
            },
            {
                "panel_id": "line_gmv",
                "kind": "chart",
                "title": "GMV 趋势",
                "subtitle": "近 30 天 · 不含税口径",
                "dataset": {"ref": "q1"},
                "chart": {"type": "line"},
                "encoding": {
                    "x": {"field": "dt", "type": "time"},
                    "y": [{"field": "gmv", "agg": "sum"}],
                },
                "style": {"area": True, "smooth": True},
                "w": 4,
                "h": 12,
            },
        ]

    async def complete(
        self,
        messages: list[dict[str, Any]],
        choice: Any,
        *,
        timeout_s: float | None = None,
        **kw: Any,
    ) -> Any:
        from dba_runtime.router import LLMResult  # noqa: PLC0415

        _ = (timeout_s, kw)
        self.calls.append(messages)
        text = self._text(messages)
        return LLMResult(
            text=text,
            model=choice.model,
            provider=self.provider,
            prompt_tokens=120,
            completion_tokens=60,
            latency_ms=1,
            finish_reason="stop",
        )

    async def stream(self, messages: list[dict[str, Any]], choice: Any, **kw: Any) -> Any:
        _ = (choice, kw)
        self.calls.append(messages)
        for ch in self._text(messages):
            yield ch


def _csv(value: str) -> set[str]:
    """逗号分隔字符串 → 去空集合。"""
    return {item.strip() for item in value.split(",") if item.strip()}


def _semantic_tables_loader(repos: Any) -> Any:
    """构造「语义层登记表」加载器（供只读护栏的表级白名单）。"""

    async def _load() -> set[str]:
        rows = await repos.sem_field_mapping.all_mappings()
        return {str(r["physical_table"]) for r in rows if r.get("physical_table")}

    return _load


# ─────────────────────────────────────────────────────────────────────
# 存储装配（L5）：可选驱动缺失 → 该组件标记不可用，不影响启动
# ─────────────────────────────────────────────────────────────────────
@dataclass
class StorageBundle:
    """存储组件聚合（任一为 ``None`` 表示该驱动不可用）。"""

    mysql: Any = None
    repos: Any = None
    redis: Any = None
    mongo: Any = None
    mongo_repos: Any = None
    milvus: Any = None
    vector: Any = None
    es: Any = None
    es_repo: Any = None
    minio: Any = None
    object_repo: Any = None
    available: dict[str, bool] = field(default_factory=dict)

    def closers(self) -> list[Any]:
        return [
            self.redis,
            self.mongo,
            self.milvus,
            self.es,
            self.minio,
            self.mysql,
        ]


def _load_component(
    bundle: StorageBundle,
    name: str,
    loader: Callable[[], Any],
    *,
    assign: Callable[[Any], None],
) -> None:
    """统一装配一个可选存储组件（缺驱动 / 连不上时**只降级、不终止启动**）。

    ``build_storage_bundle`` 里 6 个组件（mysql/redis/mongo/milvus/es/minio）此前
    各写一遍「import → 构造 → 赋值 → 记 available」，重复 6 次且容易漏改
    （例如新加组件时忘了在异常分支写 ``available=False``，``/ready`` 就会误报可用）。

    参数：
      * ``bundle``：待填充的存储聚合；
      * ``name``：``available`` / ``/ready`` 中的组件名（如 ``"mysql"``）；
      * ``loader``：真正构造实现的**惰性回调**（内部做延迟 import，缺依赖即抛异常）；
      * ``assign``：把构造结果挂到 ``bundle`` 对应字段上的回调。

    行为：成功 → ``available[name]=True``；任何异常 → ``available[name]=False`` 并记 warning。
    注意：这里**吞掉所有异常**是有意为之——可选驱动缺失绝不能让整个应用起不来（§7.6）。
    """
    try:
        assign(loader())
        bundle.available[name] = True
    except Exception as exc:  # noqa: BLE001 - 可选组件缺失必须降级而非崩溃
        bundle.available[name] = False
        logger.warning("%s 存储不可用（降级）：%s", name, exc)


def build_storage_bundle(settings: Settings) -> StorageBundle:
    """装配存储组件；每个组件独立 try，缺驱动只记录不可用。"""
    bundle = StorageBundle()
    use_fake_redis = settings.redis_use_fake or settings.env == "test"

    # MySQL（含 21 表 Repository）
    def _make_mysql() -> Any:
        from .storage.mysql.engine import MySqlStorage  # noqa: PLC0415
        from .storage.mysql.repo import MysqlRepositories  # noqa: PLC0415

        storage = MySqlStorage(rw_dsn=settings.mysql_dsn, ro_dsn=settings.mysql_ro_dsn)
        bundle.repos = MysqlRepositories(storage.rw_engine)
        return storage

    _load_component(bundle, "mysql", _make_mysql, assign=lambda v: setattr(bundle, "mysql", v))

    # Redis（预算快路径 / 进度 / 限流）
    def _make_redis() -> Any:
        from .storage.redis.client import RedisStorage  # noqa: PLC0415

        return RedisStorage(settings.redis_dsn, use_fake=use_fake_redis)

    _load_component(bundle, "redis", _make_redis, assign=lambda v: setattr(bundle, "redis", v))

    # MongoDB
    def _make_mongo() -> Any:
        from .storage.mongo.client import MongoStorage  # noqa: PLC0415
        from .storage.mongo.repo import MongoRepositories  # noqa: PLC0415

        mongo = MongoStorage(settings.mongo_dsn, settings.mongo_db)
        bundle.mongo_repos = MongoRepositories(mongo)
        return mongo

    _load_component(bundle, "mongodb", _make_mongo, assign=lambda v: setattr(bundle, "mongo", v))

    # Milvus
    def _make_milvus() -> Any:
        from .storage.milvus.client import MilvusStorage  # noqa: PLC0415
        from .storage.milvus.repo import VectorRepo  # noqa: PLC0415

        milvus = MilvusStorage(
            settings.milvus_host, settings.milvus_port, dim=settings.embedding_dim
        )
        bundle.vector = VectorRepo(milvus)
        return milvus

    _load_component(bundle, "milvus", _make_milvus, assign=lambda v: setattr(bundle, "milvus", v))

    # Elasticsearch
    def _make_es() -> Any:
        from .storage.es.client import EsStorage  # noqa: PLC0415
        from .storage.es.repo import EventIndexRepo  # noqa: PLC0415

        es = EsStorage(settings.es_url, prefix=settings.es_index_prefix)
        bundle.es_repo = EventIndexRepo(es)
        return es

    _load_component(bundle, "elasticsearch", _make_es, assign=lambda v: setattr(bundle, "es", v))

    # MinIO
    def _make_minio() -> Any:
        from .storage.minio.client import MinioStorage  # noqa: PLC0415
        from .storage.minio.repo import ObjectStoreRepo  # noqa: PLC0415

        minio = MinioStorage(
            settings.minio_endpoint,
            settings.minio_access_key,
            settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bundle.object_repo = ObjectStoreRepo(minio)
        return minio

    _load_component(bundle, "minio", _make_minio, assign=lambda v: setattr(bundle, "minio", v))

    return bundle


class CompositeHealthProbe:
    """组合探测：优先真实 ping，缺失驱动的组件退回 TCP 可达性。"""

    def __init__(self, bundle: StorageBundle, tcp: TcpHealthProbe) -> None:
        self._bundle = bundle
        self._tcp = tcp

    async def probe(self) -> dict[str, dict[str, Any]]:
        results = await self._tcp.probe()
        checks = {
            "mysql": self._check_mysql,
            "redis": self._check_redis,
            "mongodb": self._check_mongo,
            "milvus": self._check_milvus,
            "elasticsearch": self._check_es,
            "minio": self._check_minio,
        }
        for name, check in checks.items():
            if name not in results:
                continue
            if self._bundle.available.get(name):
                try:
                    ok = bool(await check())
                    results[name] = {"ok": ok, "driver": "real"}
                except Exception as exc:  # noqa: BLE001
                    results[name] = {"ok": False, "error": str(exc), "driver": "real"}
        for name, ok in self._bundle.available.items():
            if name not in results:
                results[name] = {"ok": ok, "driver": "real"}
        return results

    async def _check_mysql(self) -> bool:
        return bool(await self._bundle.mysql.ping())

    async def _check_redis(self) -> bool:
        return bool(await self._bundle.redis.ping())

    async def _check_mongo(self) -> bool:
        return bool(await self._bundle.mongo.ping())

    async def _check_milvus(self) -> bool:
        return bool(await self._bundle.milvus.ping())

    async def _check_es(self) -> bool:
        return bool(await self._bundle.es.ping())

    async def _check_minio(self) -> bool:
        return bool(await self._bundle.minio.ping())


# ─────────────────────────────────────────────────────────────────────
# 容器
# ─────────────────────────────────────────────────────────────────────
class Container:
    """极简 DI 容器：register / override / get（带单例缓存）。"""

    def __init__(self) -> None:
        self._factories: dict[Key, Factory] = {}
        self._singletons: dict[Key, Any] = {}
        self._overrides: dict[Key, Any] = {}

    def register(self, key: Key, factory: Factory) -> None:
        self._factories[key] = factory

    def set(self, key: Key, instance: Any) -> None:
        """直接登记单例（装配阶段用）。"""
        self._singletons[key] = instance

    def override(self, key: Key, instance: Any) -> None:
        """测试用：直接注入实例（绕过工厂）。"""
        self._overrides[key] = instance

    def has(self, key: Key) -> bool:
        return key in self._overrides or key in self._factories or key in self._singletons

    def get(self, key: Key, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if key in self._singletons:
            return self._singletons[key]
        factory = self._factories.get(key)
        if factory is None:
            return default
        instance = factory()
        self._singletons[key] = instance
        return instance

    async def aclose(self) -> None:
        """关闭容器内可关闭资源。"""
        for instance in list(self._singletons.values()):
            closer: Callable[[], Awaitable[Any]] | None = getattr(instance, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    logger.warning("关闭容器资源失败", exc_info=True)


def _filter_columns(payload: dict[str, Any], table: Any) -> dict[str, Any]:
    """把 payload 过滤成**目标表的列**。

    ★ 为什么必须过滤：内核 ``LLMCallRecord`` 含 ``parent_span_id`` 等**不属于** ``llm_call``
    表列的字段（span 树字段在 Mongo ``run_doc``，不在 MySQL 明细）。直接 INSERT 会因
    「未知列」报错，埋点被丢进 DLQ —— 静默降级为「成本明细缺失」。故此处按表列白名单裁剪。
    """
    allowed = {c.name for c in table.__table__.columns}
    return {k: v for k, v in payload.items() if k in allowed}


def _build_outbox_handlers(
    bundle: StorageBundle,
) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
    """构造 outbox ``kind → handler`` 映射（写 MySQL 明细 / Mongo span / ES）。"""
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
    repos = bundle.repos
    if repos is not None:
        from .storage.mysql import models as mysql_models  # noqa: PLC0415

        async def _llm(payload: dict[str, Any]) -> None:
            await repos.llm_call.bulk_insert([_filter_columns(payload, mysql_models.LlmCall)])

        async def _tool(payload: dict[str, Any]) -> None:
            await repos.tool_call.bulk_insert([_filter_columns(payload, mysql_models.ToolCall)])

        async def _run(payload: dict[str, Any]) -> None:
            """Run 生命周期：``op=finish`` 走收尾更新，否则插入 running 行。"""
            trace_id = str(payload.get("trace_id") or "")
            if not trace_id:
                return
            # outbox payload 是 JSON，datetime 已序列化为 ISO 字符串 → 解析回 datetime
            for key in ("started_at", "ended_at"):
                val = payload.get(key)
                if isinstance(val, str) and val:
                    payload[key] = datetime.fromisoformat(val)
            if payload.get("op") == "finish":
                row = _filter_columns(payload, mysql_models.Run)
                row.pop("trace_id", None)
                row.pop("started_at", None)
                # ★ R1：回填 run 汇总（tokens / cost / llm_calls）。
                #   llm_call 事件在流水线期间入队、finish 在 finally 入队 → outbox 按 id
                #   顺序投递，llm_call 先落库，此处聚合可读到；偶发读不到则保持 0，
                #   由下一次 rollup 重算兜底，不把埋点失败升级成 Run 失败。
                try:
                    agg = await repos.llm_call.aggregate_by_trace(trace_id)
                    row.update(
                        {
                            "tokens_in": agg["tokens_in"],
                            "tokens_out": agg["tokens_out"],
                            "cached_tokens": agg["cached_tokens"],
                            "cost_micro_usd": agg["cost_micro_usd"],
                            "llm_calls": agg["llm_calls"],
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - 回填失败不影响收尾
                    logger.warning("run 汇总回填失败（保持 0，下次 rollup 重算）：%s", exc)
                await repos.run.finish(trace_id, started_at=payload.get("started_at"), row=row)
            else:
                await repos.run.insert(_filter_columns(payload, mysql_models.Run))

        handlers["llm"] = _llm
        handlers["tool"] = _tool
        handlers["run"] = _run

    mongo_repos = bundle.mongo_repos
    es_repo = bundle.es_repo
    if mongo_repos is not None:

        async def _span(payload: dict[str, Any]) -> None:
            trace_id = str(payload.get("trace_id", ""))
            if not trace_id:
                return
            doc = await mongo_repos.run_doc.get(trace_id) or {
                "trace_id": trace_id,
                "spans": [],
            }
            # ★ Mongo 读回的 doc 含不可变的 ``_id``，直接 upsert 会撞
            #   "update on the path '_id' would modify the immutable field '_id'"
            #   → 曾导致 221 行 span 全进 DLQ。此处剔除再写。
            doc.pop("_id", None)
            spans = list(doc.get("spans") or [])
            spans.append(payload)
            doc["spans"] = spans
            await mongo_repos.run_doc.upsert(doc)

        async def _run_doc(payload: dict[str, Any]) -> None:
            await mongo_repos.run_doc.upsert(payload)

        handlers["span"] = _span
        handlers["run_doc"] = _run_doc

    if es_repo is not None:

        async def _es_event(payload: dict[str, Any]) -> None:
            await es_repo.index_run_event(payload)

        handlers["es_event"] = _es_event
    return handlers


def build_container(settings: Settings) -> Container:
    """装配平台依赖（存储 L5 + 能力 L4 + 内核绑定点）。"""
    container = Container()
    bundle = build_storage_bundle(settings)
    container.set("storage", bundle)

    # ── L5：存储 ──────────────────────────────────────────────
    container.set("mysql", bundle.mysql)
    container.set("repos", bundle.repos)
    container.set("redis", bundle.redis)
    container.set("mongo", bundle.mongo)
    container.set("mongo_repos", bundle.mongo_repos)
    container.set("vector", bundle.vector)
    container.set("es", bundle.es)
    container.set("object_store", bundle.object_repo)
    container.set("storage_available", dict(bundle.available))

    # 就绪探测（真实 ping + TCP 兜底）
    container.set(
        "health_probe",
        CompositeHealthProbe(bundle, TcpHealthProbe(settings.ready_components())),
    )

    # ── L4：成本 / 埋点 ───────────────────────────────────────
    from .capabilities.cost import CostNormalizer, PriceCache  # noqa: PLC0415
    from .capabilities.telemetry import (  # noqa: PLC0415
        LocalDlqWriter,
        OutboxDispatcher,
        OutboxMeteringService,
    )

    dlq = LocalDlqWriter(settings.dlq_path)
    price_cache = PriceCache()
    container.set("price_cache", price_cache)
    container.set("cost_normalizer", CostNormalizer(price_cache))
    container.set("dlq", dlq)

    metering: Any
    dispatcher: Any
    if bundle.repos is not None:
        metering = OutboxMeteringService(bundle.repos.outbox, dlq=dlq)
        dispatcher = OutboxDispatcher(
            bundle.repos.outbox,
            _build_outbox_handlers(bundle),
            dlq=dlq,
            max_attempts=settings.outbox_max_attempts,
        )
    else:
        metering = PlaceholderMeteringService()
        dispatcher = None
    container.set("metering", metering)
    container.set("dispatcher", dispatcher)

    # ── L4：预算 ──────────────────────────────────────────────
    budget_service: Any | None = None
    if bundle.repos is not None:
        from .capabilities.budget import BudgetService  # noqa: PLC0415
        from .storage.redis.repo import RedisBudgetCache  # noqa: PLC0415

        redis_cache = RedisBudgetCache(bundle.redis) if bundle.redis is not None else None
        budget_service = BudgetService(
            budget_repo=bundle.repos.budget,
            usage_repo=bundle.repos.budget_usage,
            reservation_repo=bundle.repos.budget_reservation,
            redis_cache=redis_cache,
            price_cache=price_cache,
            fail_open=settings.guardrail_degrade_policy == "fail_open_mysql",
        )
    container.set("budget", budget_service)

    # ── L4：记忆 / 技能 / 语义 / 路由 / 向量化 / 消息 ─────────
    memory_service: Any | None = None
    if bundle.mongo_repos is not None:
        from .capabilities.memory import MemoryService  # noqa: PLC0415

        memory_service = MemoryService(
            cache_repo=bundle.mongo_repos.semantic_cache_entry,
            vector_repo=bundle.vector,
            ttl_s=settings.memory_ttl_s,
        )
    container.set("memory", memory_service)

    skills_service: Any | None = None
    if bundle.repos is not None:
        from .capabilities.skills import SkillService  # noqa: PLC0415

        skills_service = SkillService(
            registry_repo=bundle.repos.skill_registry,
            usage_repo=bundle.repos.skill_usage,
            vector_repo=bundle.vector,
        )
    container.set("skills", skills_service)

    semantics_service: Any | None = None
    if bundle.repos is not None:
        from .capabilities.semantics import SemanticService  # noqa: PLC0415

        semantics_service = SemanticService(
            metric_repo=bundle.repos.sem_metric,
            field_repo=bundle.repos.sem_field_mapping,
            dict_repo=bundle.repos.sem_dict_entry,
            role_repo=bundle.repos.auth_role,
            scope_rule_repo=bundle.repos.row_scope_rule,
            vector_repo=bundle.vector,
        )
    container.set("semantics", semantics_service)

    from .capabilities.routing import ModelRouter, build_ladder  # noqa: PLC0415

    container.set(
        "model_router",
        ModelRouter(
            ladder=build_ladder(
                settings.llm_provider,
                premium=settings.llm_model_premium,
                standard=settings.llm_model_standard,
                economy=settings.llm_model_economy,
            )
        ),
    )

    from .capabilities.embedding import (  # noqa: PLC0415
        HashEmbedder,
        HttpEmbedder,
        LocalEmbedder,
    )

    embedder: Any
    if settings.embedding_use_fake or settings.env == "test":
        embedder = HashEmbedder(dim=settings.embedding_dim)
    elif settings.embedding_backend == "local":
        embedder = LocalEmbedder(
            settings.embedding_model_path,
            dim=settings.embedding_dim,
            device=settings.embedding_device,
        )
    else:
        embedder = HttpEmbedder(
            settings.embedding_url,
            path=settings.embedding_path,
            dim=settings.embedding_dim,
            model=settings.embedding_model,
        )
    container.set("embedder", embedder)

    from .capabilities.messaging import ProgressPublisher  # noqa: PLC0415

    if bundle.redis is not None:
        from .storage.redis.repo import RedisProgressWriter  # noqa: PLC0415

        progress_writer: Any = RedisProgressWriter(bundle.redis)
    else:
        progress_writer = InMemoryProgressWriter()
    container.set("progress", progress_writer)
    container.set("progress_publisher", ProgressPublisher(progress_writer))

    # SSE 订阅票据（★ P0-7：EventSource 带不上 JWT，改为先 POST 拿 ticket 再订阅）
    from .api.stream_tickets import StreamTicketStore  # noqa: PLC0415

    container.set("stream_tickets", StreamTicketStore(ttl_s=60))

    # 限流（Redis 有则分布式，否则进程内兜底）
    if bundle.redis is not None:
        from .storage.redis.repo import RedisRateLimiter  # noqa: PLC0415

        container.set("rate_limiter", RedisRateLimiter(bundle.redis))
        from .storage.redis.repo import RedisIdempotency  # noqa: PLC0415

        container.set("idempotency", RedisIdempotency(bundle.redis))
    else:
        container.set("rate_limiter", None)
        container.set("idempotency", None)

    # 审计落点（ES 有则写 ES，否则内存）
    if bundle.es_repo is not None:
        container.set("audit_sink", bundle.es_repo)
    else:
        container.set("audit_sink", InMemoryAuditSink())

    # ── L4：路由网关（内核 ModelRouter Protocol 的实现）────────────
    from .capabilities.routing import ModelGateway, ModelRouter  # noqa: PLC0415

    llm = _build_llm(settings)
    gateway = ModelGateway(
        router=container.get("model_router") or ModelRouter(),
        llm=llm,
        budget=budget_service,
        cost=container.get("cost_normalizer"),
    )
    container.set("llm", llm)
    container.set("model_gateway", gateway)

    # ── L3：ChatBI 主链路（护栏链 + 唯一执行入口 + 七步流水线）──────
    if bundle.repos is not None and bundle.mysql is not None:
        from .modules.chatbi import build_chatbi  # noqa: PLC0415

        allowed = _csv(settings.chatbi_allowed_tables)
        extra = _csv(settings.chatbi_extra_functions)
        chatbi = build_chatbi(
            audit=bundle.repos.sql_audit,
            scope_rule_repo=bundle.repos.row_scope_rule,
            field_mapping_repo=bundle.repos.sem_field_mapping,
            readonly_pool=bundle.mysql.read_only,
            router=gateway,
            semantic=semantics_service,
            memory=memory_service,
            skill=skills_service,
            object_store=bundle.object_repo,
            allowed_tables=allowed or None,
            extra_functions=extra or None,
            table_loader=_semantic_tables_loader(bundle.repos),
            max_rows=settings.chatbi_max_rows,
            timeout_s=settings.chatbi_sql_timeout_s,
            inline_threshold=settings.chatbi_inline_row_threshold,
            bucket=settings.chatbi_result_bucket,
        )
        container.set("chatbi", chatbi)
        container.set("query_executor", chatbi.executor)
        container.set("chatbi_pipeline", chatbi.pipeline)
        container.set("chatbi_registry", chatbi.registry)
        container.set("guard_chain", chatbi.guard_chain)
        container.set("spec_builder", chatbi.spec_builder)
    else:
        # MySQL 不可用 → ChatBI 无法取数（诚实降级，不假装可用）
        container.set("chatbi", None)
        logger.warning("ChatBI 主链路未装配（MySQL 不可用）")

    # ── L3：模块 03 observability（四角色流水线 + 只读服务）────────
    observability_service: Any | None = None
    if bundle.repos is not None:
        from .modules.observability import build_observability  # noqa: PLC0415

        report_repo = bundle.mongo_repos.anomaly_report if bundle.mongo_repos else None
        obs = build_observability(
            alert_repo=bundle.repos.alert_event,
            metric_repo=bundle.repos.metric_daily,
            agent_repo=bundle.repos.app_agent,
            run_repo=bundle.repos.run,
            report_repo=report_repo,
            skills=skills_service,
            memory=memory_service,
            metering=metering,
        )
        container.set("observability", obs)
        container.set("observability_service", obs.service)
        observability_service = obs.service
    else:
        container.set("observability", None)
        container.set("observability_service", None)
        logger.warning("Observability 模块未装配（MySQL 不可用）")

    # ── L3：模块 10 finops（四角色流水线 + 只读服务）──────────────
    if bundle.repos is not None:
        from .capabilities.budget import GuardrailPolicy  # noqa: PLC0415
        from .modules.finops import build_finops  # noqa: PLC0415
        from .modules.finops.sources import (  # noqa: PLC0415
            build_cache_source,
            build_coverage_source,
            build_detail_source,
        )

        finops = build_finops(
            observability=observability_service,
            budget=budget_service,
            reco_repo=(bundle.mongo_repos.finops_recommendation if bundle.mongo_repos else None),
            alert_repo=bundle.repos.alert_event,
            cost_normalizer=container.get("cost_normalizer"),
            coverage_source=build_coverage_source(bundle.repos),
            detail_source=build_detail_source(bundle.repos),
            cache_source=build_cache_source(bundle.repos),
            policy=GuardrailPolicy.from_dict(settings.guardrail_policy()),
        )
        container.set("finops", finops)
        container.set("finops_service", finops.service)
    else:
        container.set("finops", None)
        container.set("finops_service", None)
        logger.warning("FinOps 模块未装配（MySQL 不可用）")

    return container


def _build_llm(settings: Settings) -> Any:
    """构造 LLM 客户端；无 Key / 测试环境 / 未放行的 dev 回退到确定性演示替身。

    ★ 三态（优先级由高到低）：
      1) ``DBA_LLM_USE_FAKE=true``                         → 恒替身（浏览器 e2e 用它锁死确定性）
      2) ``DBA_ENV=test`` 或 API Key 为空                  → 替身
      3) ``DBA_ENV=dev`` 且未设 ``DBA_LLM_ALLOW_DEV=true`` → 替身（开发期默认不打付费 API）
      4) 否则                                              → 真实模型

    返回：``OpenAIClient`` 或 ``DemoLLMClient``（降级替身，**不是**真实模型）。
    """
    use_fake = (
        settings.llm_use_fake
        or settings.env == "test"
        or not settings.llm_api_key
        or (settings.env == "dev" and not settings.llm_allow_dev)
    )
    if use_fake:
        return DemoLLMClient()
    try:
        from dba_runtime.llm.openai_client import OpenAIClient  # noqa: PLC0415

        return OpenAIClient(
            api_key=settings.llm_api_key or None,
            base_url=settings.llm_base_url or None,
            provider=settings.llm_provider,
        )
    except Exception as exc:  # noqa: BLE001 - 缺 dba[llm] 依赖时降级（并告警）
        logger.warning("真实 LLM 客户端不可用，降级为演示替身：%s", exc)
        return DemoLLMClient()


async def warmup(container: Container) -> None:
    """异步预热：价格缓存 / Redis 脚本 / 可选集合初始化。

    在 lifespan 中于 ``bind_metering`` **之前**调用：
    * 预热 ``price_cache``（``CostNormalizer`` 是同步的，必须提前载入价格）；
    * 注册 Redis Lua 脚本（预算预留/结算/释放）；
    * 若存储可用，``ensure_*`` 幂等初始化（不存在则创建）。
    """
    bundle: StorageBundle = container.get("storage")
    repos = bundle.repos

    price_cache = container.get("price_cache")
    if price_cache is not None and repos is not None:
        try:
            await price_cache.refresh(repos.price_book)
            logger.info("price_cache 预热 %d 条", len(price_cache))
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻塞启动（normalize 返回 unknown）
            logger.warning("price_cache 预热失败：%s", exc)

    if bundle.redis is not None:
        try:
            await bundle.redis.ensure_scripts()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis Lua 脚本注册失败：%s", exc)
