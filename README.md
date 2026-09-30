# dba-platform

数据大屏 × 多 Agent 平台（三模块：ChatBI / Observability / FinOps）。

本仓库是 **uv workspace** 单体仓库：

```
packages/agent-runtime   # 可复用运行时内核（L2，零外部依赖）
apps/platform            # 平台单体应用（FastAPI + 三模块）
apps/worker              # 常驻定时任务
deploy/                  # docker-compose / Dockerfile
migrations/              # Alembic
```

## 快速开始

> ★ 外部组件（MySQL / MongoDB / Redis / Milvus / Elasticsearch / MinIO / Embedding）
> 已迁到**虚拟机 192.168.200.10**，本机不再用 `docker-compose` 拉起。首次使用请在
> 虚拟机上执行 `bash deploy/vm-provision.sh` 准备好 MySQL/Redis/Embedding，详见
> `deploy/VM_SETUP.md`。

```bash
uv sync --all-packages --extra prod          # 安装（含全量存储驱动）
cp .env.example .env                          # 按需覆盖（默认已指向 192.168.200.10）
uv run dba migrate                            # Alembic 升级 MySQL
uv run dba bootstrap-storage                  # 建 Milvus/ES/MinIO 资源
uv run dba seed --demo                        # 灌入口径/技能/价格表/演示数据
uv run uvicorn dba.main:app --reload --port 8000
```

> `deploy/docker-compose.yml` 已改为**参考 / 备用**（本地全栈回滚用），默认不启用。

## 质量

```bash
uv run ruff check .
uv run mypy packages apps
uv run pytest -q
```

> 当前进度：**B0（骨架/基础设施）+ B1（运行时内核 `dba_runtime`）** 已交付。
> B2~B6（存储适配层 / L4 能力层 / 三模块业务 / 前端 / 评测）按批次推进。
