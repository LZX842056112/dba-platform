"""配置（pydantic-settings 分层 .env）。

对齐《设计文档 v2》§3.1 / §3.4 与《实现要点清单》§1.2.6.2。

约定：
* 环境变量前缀 ``DBA_``（与 ``.env.example`` 一致）；
* ``DBA_ENV`` 选择 base/dev/prod 覆盖层：先读 ``.env``，再读 ``.env.{env}``（后者优先）；
* ``guardrail_policy()`` 暴露预算守卫护栏配置（默认只告警、硬熔断默认关闭，§6.5）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

#: ★ 外部组件所在虚拟机。MySQL / MongoDB / Redis / Milvus / Elasticsearch / MinIO /
#: Embedding 全部迁到此地址；本机不再运行这些组件。
#: 改地址只需改这一处 + ``.env.example`` + ``deploy/vm-provision.sh`` 顶部的变量。
_VM_HOST = "192.168.200.10"


class Settings(BaseSettings):
    """平台配置。所有金额字段为 micro_usd 整数、时间一律 UTC。"""

    model_config = SettingsConfigDict(
        env_prefix="DBA_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["dev", "prod", "test"] = "dev"

    # ── 存储（★ 外部组件已迁到虚拟机 _VM_HOST:192.168.200.10）─────────
    # 账号/密码与 deploy/vm-provision.sh 顶部变量保持一致，便于对照修改。
    mysql_dsn: str = f"mysql+asyncmy://dba_user:dba_user_pwd_2026@{_VM_HOST}:3306/dba"
    mysql_ro_dsn: str = (
        f"mysql+asyncmy://dba_readonly:dba_readonly_pwd_2026@{_VM_HOST}:3306/dba"
    )
    mongo_dsn: str = f"mongodb://{_VM_HOST}:27017"
    mongo_db: str = "dba"
    redis_dsn: str = f"redis://:960802@{_VM_HOST}:6379/0"
    milvus_host: str = _VM_HOST
    milvus_port: int = 19530
    es_url: str = f"http://{_VM_HOST}:9200"
    es_index_prefix: str = "dba"
    minio_endpoint: str = f"{_VM_HOST}:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False

    # ── 向量化服务（★ 复用 VM 上已有的 8081 服务，模型 bge-large-zh-v1.5，dim=1024）──
    embedding_url: str = f"http://{_VM_HOST}:8081"   # base URL（不含路径）
    embedding_path: str = "/v1/embeddings"           # OpenAI 兼容端点（自建 bge-m3 时为 "/embed"）
    embedding_dim: int = 1024
    embedding_model: str = "bge-large-zh-v1.5"
    #: 为 True 时用确定性 HashEmbedder（离线/单测），不访问外部向量服务
    embedding_use_fake: bool = False

    # ── 缓存 / 投递 ─────────────────────────────────────────
    #: 语义缓存 TTL（秒）
    memory_ttl_s: int = 3600
    #: 投递失败 DLQ 落盘路径（JSONL）
    dlq_path: str = "var/dlq/telemetry.jsonl"
    #: 测试环境用 fakeredis（真实 Lua）
    redis_use_fake: bool = False
    #: outbox 投递最大重试次数（超过落 DLQ）
    outbox_max_attempts: int = 5

    # ── 安全 ────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-prod"
    jwt_alg: str = "HS256"
    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 1209600

    # ── 预算守卫护栏（§6.5）──────────────────────────────────
    guardrail_enabled: bool = True
    guardrail_allow_downgrade: bool = True
    guardrail_allow_compress: bool = True
    guardrail_allow_rate_limit: bool = True
    guardrail_allow_circuit_break: bool = False  # ★ 硬熔断默认关闭
    guardrail_exemption_priority: int = 50
    guardrail_breaker_window_s: int = 60
    guardrail_breaker_consecutive_windows: int = 3
    guardrail_breaker_cooldown_s: int = 300
    guardrail_max_downgrades_per_run: int = 2
    guardrail_kill_switch: bool = False
    guardrail_degrade_policy: Literal["fail_open_mysql", "fail_closed"] = "fail_open_mysql"

    # ── LLM ─────────────────────────────────────────────────
    llm_provider: str = "openai"
    llm_base_url: str = ""
    llm_api_key: str = ""
    #: 为 True 时用内核 DeterministicLLM（离线/单测/演示），不访问外部模型
    llm_use_fake: bool = False

    # ── ChatBI 主链路（SQL 护栏 / 执行）─────────────────────
    #: 单次查询行数上限（超限截断并置 truncated=True）
    chatbi_max_rows: int = 5000
    #: SQL 执行超时（秒）：SQL 侧 MAX_EXECUTION_TIME 提示 + 应用侧 asyncio.timeout 双保险
    chatbi_sql_timeout_s: float = 30.0
    #: 大屏数据源内联阈值（超过则走 MinIO 预签名 URL）
    chatbi_inline_row_threshold: int = 500
    #: 结果对象存储 bucket
    chatbi_result_bucket: str = "dba-query-results"
    #: 物理表白名单（逗号分隔）。空 = 不限制（仅开发）；生产**必须**配置，
    #: 未配置时只读护栏会记警告并以「语义层登记表」兜底。
    chatbi_allowed_tables: str = ""
    #: 额外允许的函数（逗号分隔，追加到内置白名单）
    chatbi_extra_functions: str = ""

    # ── 平台自身 ────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    # 整次 Run 的墙钟预算（秒）；None/0 表示不限制
    run_deadline_s: float = 180.0

    def guardrail_policy(self) -> dict[str, Any]:
        """预算守卫护栏配置（B5 的 ``GuardrailPolicy`` 由本字典构造）。"""
        return {
            "enabled": self.guardrail_enabled,
            "allow_downgrade": self.guardrail_allow_downgrade,
            "allow_compress": self.guardrail_allow_compress,
            "allow_rate_limit": self.guardrail_allow_rate_limit,
            "allow_circuit_break": self.guardrail_allow_circuit_break,
            "exemption_priority": self.guardrail_exemption_priority,
            "breaker_window_s": self.guardrail_breaker_window_s,
            "breaker_consecutive_windows": self.guardrail_breaker_consecutive_windows,
            "breaker_cooldown_s": self.guardrail_breaker_cooldown_s,
            "max_downgrades_per_run": self.guardrail_max_downgrades_per_run,
            "kill_switch": self.guardrail_kill_switch,
            "degrade_policy": self.guardrail_degrade_policy,
        }

    def ready_components(self) -> dict[str, tuple[str, int]]:
        """``/ready`` 探测目标：组件名 → (host, port)。

        硬依赖：mysql / mongodb / redis；可降级：milvus / elasticsearch / minio / embedding。
        端点从 DSN / URL 解析（B0 用 TCP 可达性探测；B2+ 换成真实 ping）。
        """
        return {
            "mysql": _host_port(self.mysql_dsn, default=(_VM_HOST, 3306)),
            "mongodb": _host_port(self.mongo_dsn, default=(_VM_HOST, 27017)),
            "redis": _host_port(self.redis_dsn, default=(_VM_HOST, 6379)),
            "milvus": (self.milvus_host, self.milvus_port),
            "elasticsearch": _host_port(self.es_url, default=(_VM_HOST, 9200)),
            "minio": _host_port(self.minio_endpoint, default=(_VM_HOST, 9000)),
            "embedding": _host_port(self.embedding_url, default=(_VM_HOST, 8081)),
        }


#: 硬依赖：只有它们全挂才返回 503，否则返回 degraded（§7.6 降级语义）
HARD_DEPENDENCIES: frozenset[str] = frozenset({"mysql", "mongodb", "redis"})


def _host_port(dsn: str, *, default: tuple[str, int]) -> tuple[str, int]:
    """从 DSN / URL 粗解析 ``host`` 与 ``port``（仅用于可达性探测）。"""
    text = dsn
    if "://" in text:
        text = text.split("://", 1)[1]
    if "@" in text:
        text = text.split("@", 1)[1]
    text = text.split("/", 1)[0]
    if not text:
        return default
    if ":" in text:
        host, _, port_s = text.partition(":")
        try:
            return host or default[0], int(port_s)
        except ValueError:
            return host or default[0], default[1]
    return text, default[1]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置。

    ★ 分层覆盖：``.env`` 先加载，再由 ``.env.{DBA_ENV}`` 覆盖（后者优先）。
    ``os.environ`` 始终优先于文件，便于容器注入。
    """
    base = Settings()
    env_layer = f".env.{base.env}"
    if os.path.exists(env_layer):
        return Settings(_env_file=(".env", env_layer))  # type: ignore[call-arg]
    return base
