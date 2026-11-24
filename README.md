# dba-platform

**数据大屏 × 多 Agent 平台**：用自然语言生成数据大屏，并对「生成过程本身」做治理。

## 项目简介

平台是一个 uv workspace 单体仓库，围绕同一个运行时内核构建三个模块：

| 模块 | 一句话职责 |
|---|---|
| **ChatBI** | 「问一句 → 出一屏」：七步流水线 + 五道 SQL 护栏 |
| **Observability** | 观测 Agent 舰队自身：指标 / 拓扑 / 异常 / 静默失败 |
| **FinOps** | 成本计量、预算守卫、复用率优化 |

三条贯穿全局的设计约束：

1. **一个 `trace_id` 对应三个视角。** 一次提问（Run）贯穿全程：ChatBI 看它是执行记录，Observability 看它是观测对象，FinOps 看它是计量对象。埋点只做一次，三个模块是同一份运行时数据的不同读法。
2. **行级权限在生成期下沉。** 权限条件在 SQL **生成阶段**注入 `WHERE`，不做「查完再过滤」——否则敏感数据已经过内存、日志与 LLM 上下文。护栏基于 AST（sqlglot）+ 合取项覆盖性断言，失败即拒绝（fail-closed）。
3. **预算守卫前置。** LLM 调用**之前**执行 `reserve → settle/release` 三段式；软限降级，硬限熔断，且**硬熔断默认关闭**（唯一会中断业务的动作，避免误配导致全面停摆）。

---

## 功能说明

### ChatBI：自然语言到数据大屏

七步流水线：

```
intent → schema_link → sql_gen → sql_guard → sql_exec → visual → narrator
```

- 失败时 `sql_gen` 可带着失败 SQL 与校验反馈自愈重生成；**越权类错误短路不重试**。
- 五道护栏按冻结顺序执行，`row_scope` 注入后紧跟一道覆盖性断言，链上合计 6 步，全部 fail-closed：
  `readonly`（单语句 / 只读 / 危险构造黑名单 / 表与函数白名单）→ `dialect`（方言往返校验）→
  `row_scope`（按 SELECT 作用域注入行级谓词）→ `row_scope_verify`（断言顶层 AND 链已含行级谓词）→
  `limit`（行数上限 + 执行超时）→ `dry_run`（只读账号 `EXPLAIN` 预执行）。
- 结果大于内联阈值时写入对象存储，面板通过预签名 URL 获取，不把大结果塞进 SSE。
- 前端逐面板渲染大屏，SQL 可折叠查看，解说文字流式输出。

### Observability：观测 Agent 舰队

四角色线性流水线：`collect → aggregate → anomaly → broadcast`。

- **无阈值异常检测**：EWMA 基线（α=0.3）+ MAD 稳健偏差（k=3.5）。用 MAD 而非标准差，避免「异常越大反而越检不出」。
- **静默失败检测**：成功但零 token 零工具调用，或耗时显著低于同类任务 P5。
- **三条价值指标**：技能复用率、死技能数、记忆命中率。
- **指标 rollup**：全量重算且幂等，重复执行结果一致。
- **调用拓扑**：Agent 与工具之间的调用关系与错误率。
- **OTLP 接入**：外部 Agent 可通过 `/obs/otel/v1/traces` 上报 span。

### FinOps：成本与预算治理

四角色线性流水线：`meter → attribute → guard → optimize`。

- **成本是派生量**：明细固化 token 与价格表版本，价格修正后可精确重算，不依赖当时算出的金额。
- **四维归因**：业务线 / Agent / 模型 / 技能。
- **预算三段式**：`reserve → settle/release`，Redis Lua 原子执行 + MySQL 权威账本，操作幂等。
- **降级动作可开关**：降级模型、压缩上下文、限流均可在配置中关闭；硬熔断默认关闭，并带豁免线与全局 kill switch。
- **死循环检测**：300s 内同一工具同一参数重复 ≥ 8 次即判定。
- **复用率-成本曲线**：带对照组，避免把自然下降算成优化收益。

### 前端页面

| 页面 | 路径 | 职责 |
|---|---|---|
| 登录 | `/login` | 用户名 + 口令换取 JWT |
| 对话 | `/` | 提问、七步进度、SQL 折叠、逐面板出图、流式解说 |
| 流程监控 | `/observability/flow` | 模块工位、七步流水线、KPI、成本趋势、实时运行流 |
| 调用拓扑 | `/observability/topology` | Agent 与工具的调用关系 |
| 运行明细 | `/observability/runs` | 最近一批 Run 的中文表格 |
| 运行详情 | `/observability/runs/:traceId` | 基本信息 + span 列表（含层级缩进） |
| 异常告警 | `/observability/anomalies` | 告警列表、确认、解决 |
| 成本总览 | `/finops` | 区间成本、模型分解、Top 花钱方、计价覆盖率、平台自身成本 |
| 复用曲线 | `/finops/reuse` | 复用率与成本的对照关系 |
| 预算与护栏 | `/finops/budgets` | 预算额度、使用量与熔断状态 |
| 优化建议 | `/finops/recommendations` | 建议列表与采纳动作 |

`/observability` 会重定向到流程监控页。顶部一级导航为「对话 / 观测 / FinOps」，观测分区内用二级页签切换四个页面。

### 接口与实时通道

- **REST**：`/api/v1` 下 68 个端点，按组划分：`auth`(6) · `chat`(9) · `dashboards`(4) · `semantics`(6) · `obs`(19) · `finops`(22) · `stream`(2)。
- **运维端点**：`/health`（存活，不查依赖）、`/ready`（逐组件检查 + `metering_bound`，三态 `ok`/`degraded`/`not_ready`）、`/metrics`（Prometheus 文本）。
- **SSE**：提问后先用 `POST /chat/stream-ticket` 换取一次性票据，再以 `?ticket=` 订阅 `GET /stream/runs/{trace_id}`。每条消息带 `id: {seq}` 支持断线续传，回放最近 200 条（Redis LIST，TTL 1h）；历史被截断时显式下发 `replay.gap`；心跳 15s，单次订阅上限 300s。
- **统一错误体**：`{"code","message","detail","trace_id"}`，响应头回写 `X-Trace-Id`。

### 命令行与定时任务

`dba` 命令：

```bash
dba migrate [--revision head] [--dry-run]        # Alembic 升级 MySQL（幂等）
dba bootstrap-storage [--only milvus|es|minio]   # 建 Milvus collection / ES index / MinIO bucket
dba seed                                         # 角色 / 价格表 / 预算 + 首个管理员（需配置环境变量）
dba reindex [--target all|milvus|es]             # 重建检索索引
```

`dba-worker` 常驻进程注册 8 个周期任务，均带 Redis 分布式锁与独立 RunContext：

| 任务 | 触发 | 作用 |
|---|---|---|
| `rollup` | 每日 02:00 | 指标日聚合写 `metric_daily` |
| `partition` | 每月 1 日 01:00 | 维护 MySQL 月分区 |
| `anomaly_scan` | 每 15 分钟 | 异常扫描并落告警 |
| `report` | 每日 07:00 | 生成运行日报 |
| `reconcile` | 每日 03:00 | 账目对账 |
| `stale_run` | 每 10 分钟 | 关闭僵死 Run |
| `semcache_gc` | 每 30 分钟 | 语义缓存回收 |
| `archive` | 每周日 04:00 | 冷数据归档 |

---

## 安装与运行

### 前置要求

| 项 | 要求 |
|---|---|
| Python | ≥ 3.12 |
| Node.js | ≥ 20 |
| 包管理 | uv（后端 workspace）· npm（前端） |
| 外部组件 | MySQL 8 / MongoDB / Redis 必选；Milvus / Elasticsearch / MinIO / Embedding 可降级 |

外部组件默认部署在独立主机（示例见 `.env.example` 中的 `192.168.200.10`），本机不拉容器。

### 后端

```bash
uv sync --all-packages --extra prod   # 安装（含全部存储驱动）
cp .env.example .env                  # 按需覆盖为真实凭据
uv run dba migrate                    # 建表 / 升级（幂等）
uv run dba bootstrap-storage          # 建 Milvus / ES / MinIO 资源
uv run dba seed                       # 角色 / 价格表 / 预算；未配置管理员变量时不建账号
uv run uvicorn dba.main:app --port 8000
```

首次部署需要创建管理员时，先在 `.env` 中配置（**不要提交真实口令**）：

```ini
DBA_BOOTSTRAP_ADMIN_USERNAME=admin
DBA_BOOTSTRAP_ADMIN_PASSWORD=<强口令>
```

然后再执行 `uv run dba seed`。两项缺一即跳过账号创建；重复执行会刷新该账号的口令哈希。

本机没有 `uv` 时可直接用虚拟环境：

```bash
.venv/Scripts/python.exe -m dba.cli migrate
.venv/Scripts/python.exe -m uvicorn dba.main:app --port 8000
```

启动后可自检：`curl http://127.0.0.1:8000/health`、`curl http://127.0.0.1:8000/ready`。

### 前端

```bash
cd frontend
npm install
npm run dev        # 开发服务器，默认 http://127.0.0.1:5173
```

Vite 已把 `/api` 代理到 `http://127.0.0.1:8000`，本地无需处理跨域。后端启动后可用 `npm run gen:api` 重新生成 `src/types/api.d.ts`。

### 账号管理

仓库不内置任何账号或口令。首次部署按上节配置 `DBA_BOOTSTRAP_ADMIN_USERNAME` /
`DBA_BOOTSTRAP_ADMIN_PASSWORD` 并执行 `dba seed`，即可创建带 `admin` 角色的首个管理员。

- 口令只以 SHA-256 摘要写入 `auth_user.password_hash`，明文不落库；登录校验只接受摘要，明文行无法登录。
- 轮换口令：更新环境变量后重跑 `dba seed`，会对同名用户刷新摘要。
- 其余用户通过语义层/管理接口创建并授予角色；`admin`、`analyst`、`viewer` 三个角色由 `dba seed` 写入。
- 不要把管理员口令写进 `.env` 以外的任何文件；`.env` 已被 gitignore。

生产部署前应确认：JWT 密钥 `DBA_JWT_SECRET` 不是默认值、Agent 上报令牌 `DBA_AGENT_TOKEN` 已设置、硬熔断 `DBA_GUARDRAIL_ALLOW_CIRCUIT_BREAK` 按实际策略配置。

---

## 配置说明

### 加载机制

- 全部环境变量前缀为 `DBA_`，由 `pydantic-settings` 读取，字段未设置时回退到代码默认值。
- 先读 `.env`，再按 `DBA_ENV` 叠加 `.env.{env}`（例如 `DBA_ENV=dev` 时叠加 `.env.dev`）。
- `.env` 已被 gitignore；不要提交真实凭据。

### 常用项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DBA_ENV` | `dev` | 环境标识，决定叠加哪个 `.env.{env}` |
| `DBA_MYSQL_DSN` | — | MySQL 主连接串（`mysql+asyncmy://`） |
| `DBA_MYSQL_RO_DSN` | — | 只读账号连接串，供护栏 dry-run 使用 |
| `DBA_MONGO_DSN` / `DBA_MONGO_DB` | — | Mongo 连接与库名（存 `run_doc`） |
| `DBA_REDIS_DSN` | — | Redis 连接（预算原子操作、SSE 回放、分布式锁） |
| `DBA_CHATBI_MAX_ROWS` | `5000` | 单次查询行数上限，超限截断 |
| `DBA_CHATBI_SQL_TIMEOUT_S` | `30` | SQL 执行超时（提示 + 应用侧双保险） |
| `DBA_CHATBI_INLINE_ROW_THRESHOLD` | `500` | 超过该行数走对象存储预签名 URL，不内联 |
| `DBA_RUN_DEADLINE_S` | `180` | 单次 Run 墙钟预算 |
| `DBA_JWT_SECRET` / `DBA_ACCESS_TOKEN_TTL_S` | — / `3600` | 登录令牌签名与有效期 |
| `DBA_AGENT_TOKEN` | 空 | Agent 上报令牌；为空时上报接口拒绝请求 |
| `DBA_GUARDRAIL_ALLOW_CIRCUIT_BREAK` | `false` | 硬熔断开关（唯一会中断业务的动作） |
| `DBA_GUARDRAIL_DEGRADE_POLICY` | `fail_open_mysql` | Redis 不可用时的兜底策略 |
| `DBA_GUARDRAIL_KILL_SWITCH` | `false` | 全局总闸 |
| `DBA_EMBEDDING_BACKEND` | `local` | 向量化后端：进程内模型或 HTTP 服务 |

完整清单以 `.env.example` 为准，那里对每组变量都写明了用途。

### 接入真实模型

未配置模型凭据时使用本地确定性替身（`DemoLLMClient`），同一问题结果稳定，便于在没有外部 API 的环境里验证流程。生产环境必须接入真实模型，在 `.env` 中配置：

```ini
DBA_LLM_PROVIDER=deepseek
DBA_LLM_BASE_URL=https://api.deepseek.com/v1   # 只填到 /v1，SDK 会自行追加 /chat/completions
DBA_LLM_API_KEY=sk-xxx
DBA_LLM_ALLOW_DEV=true                          # dev 默认走替身，置 true 才用真实模型
```

**LLM 三态开关（优先级由高到低）：**

1. `DBA_LLM_USE_FAKE=true` → 恒用替身；
2. `DBA_ENV=test` 或 API Key 为空 → 替身；
3. `DBA_ENV=dev` 且未设 `DBA_LLM_ALLOW_DEV=true` → 替身（避免开发期误打付费 API）；
4. 否则 → 真实模型。

真实模型依赖语义层才知道表结构，生产环境需先通过语义层接口登记指标口径与字段映射，再开放问答入口。

### 组件降级

Milvus / Elasticsearch / MinIO / Embedding 属于**可选组件**：未配置或不可用时服务照常启动，依赖它们的能力自动跳过，`/ready` 会把这些组件标记为 `degraded`（HTTP 200）。例如对象存储缺失时，超过内联阈值的结果只保留 `s3` 引用、不生成预签名 URL，面板会提示「结果未内联」。

MySQL / MongoDB / Redis 是**硬依赖**，任一不可用时 `/ready` 返回 `not_ready`（HTTP 503），服务不接流量。

---

## 目录结构

```
dba-platform/
├── packages/agent-runtime/          # L2 可复用运行时内核（与业务无关）
│   └── src/dba_runtime/
│       ├── pipeline.py              #   流水线调度与自愈状态机
│       ├── context.py               #   RunContext：trace_id / span / 业务线
│       ├── telemetry.py             #   埋点装饰器（agent / tool / metered）
│       ├── budget.py                #   预算协议
│       ├── events.py                #   SSE 事件模型
│       ├── llm/                     #   LLM 网关与客户端
│       ├── tools/                   #   工具注册与调用
│       └── registry.py router.py skills.py memory.py agent.py errors.py
│
├── apps/platform/                   # L1 平台应用（FastAPI）
│   └── src/dba/
│       ├── main.py                  #   应用装配、中间件、运维端点
│       ├── config.py                #   Settings（DBA_ 前缀）
│       ├── di.py                    #   依赖注入容器
│       ├── cli.py                   #   dba 命令行
│       ├── api/                     #   接入层：路由 / 中间件 / 统一错误体
│       ├── modules/                 #   领域层：chatbi / observability / finops
│       │   ├── chatbi/              #     七步流水线、五道护栏、prompts/
│       │   ├── observability/       #     采集 / 聚合 / 异常检测 / 拓扑
│       │   └── finops/              #     计量 / 归因 / 预算守卫 / 优化
│       ├── capabilities/            #   能力层：budget cost embedding memory messaging
│       │                            #            routing semantics skills telemetry
│       ├── storage/                 #   存储适配：mysql mongo redis es milvus minio
│       ├── schemas/                 #   大屏 JSON 与 DTO schema
│       └── util/                    #   时间等通用工具
│
├── apps/worker/                     # 常驻定时任务
│   └── src/dba_worker/
│       ├── scheduler.py             #   8 个周期任务注册
│       └── jobs/                    #   rollup / partition / anomaly_scan / report
│                                    #   reconcile / stale_run / semcache_gc / archive
│
├── frontend/                        # React 18 + Vite 7 + TypeScript
│   └── src/
│       ├── app/                     #   路由、外壳、分区页签、错误边界
│       ├── pages/                   #   登录 / 对话 / 观测 4 页 / FinOps 4 页
│       ├── features/                #   按业务域聚合的 api / hooks / store / 组件
│       ├── components/              #   通用组件（图表、页面骨架、状态位）
│       ├── api/                     #   HTTP 客户端与 SSE 客户端
│       ├── lib/                     #   格式化、ECharts 注册、大屏校验
│       ├── styles/                  #   全局样式与驾驶舱样式
│       └── types/                   #   前端类型与事件协议
│
├── migrations/versions/             # Alembic 迁移脚本
├── deploy/                          # Dockerfile 与本地 embedding 服务
├── scripts/seed.py                  # 造数实现（dba seed 的落地）
├── .github/workflows/ci.yml         # CI：依赖解析 + 前端类型检查与构建
├── .env.example                     # 配置模板
├── pyproject.toml                   # uv workspace 定义
└── uv.lock
```

### 分层边界

```
L1 接入 api/  →  L3 领域 modules/  →  L4 能力 capabilities/  →  L5 存储 storage/
                        ↓
              L2 内核 packages/agent-runtime
```

- **L3 模块之间不互相 import。** 跨模块协作只能经 L4 能力层或 `trace_id` 软关联，例如 FinOps 的成本时序必须经 `ObservabilityService.timeseries` 读取。
- **L5 只做适配**，不含业务判断；L4 提供原子能力，不含页面语义。
- 内核（L2）不得 import 平台（L1）的任何模块。

---

## 开发使用说明

### 常用命令

| 场景 | 命令 |
|---|---|
| 后端开发启动 | `uv run uvicorn dba.main:app --reload --port 8000` |
| 数据库变更 | 新建 `migrations/versions/xxx.py` 后 `uv run dba migrate` |
| 灌入基础种子 | `uv run dba seed`（创建管理员需先配置环境变量） |
| 前端开发 | `cd frontend && npm run dev` |
| 前端类型检查 | `cd frontend && npm run lint` |
| 前端构建 | `cd frontend && npm run build` |
| 重新生成接口类型 | `cd frontend && npm run gen:api`（需后端已启动） |
| 定时任务常驻 | `uv run dba-worker`（等价 `uv run python -m dba_worker`） |
| 调试单个任务 | 直接调用 `dba_worker.jobs.<模块>` 里的 `run_*` 函数，首参为容器：`asyncio.run(run_rollup(container))` |

### 新增一个接口

1. 在 `apps/platform/src/dba/api/v1/<domain>.py` 添加路由；鉴权用 `Depends(get_current_principal)`。
2. 业务逻辑放在 `modules/<domain>/service.py`，路由层只做参数解析与降级处理。
3. 存储访问通过 `storage/` 的 Repo，不在路由或 service 里直接写 SQL。
4. 需要新表时补 `migrations/versions/` 脚本，表名与字段遵循现有命名。
5. 前端在 `features/<domain>/api.ts` 加类型化 getter，页面不要直接拼 URL。

### 新增一个前端页面

1. 在 `pages/` 下建目录与页面组件，页面只负责布局与交互。
2. 数据获取走 `features/` 下的 api + `useQuery`，查询键按 `<domain>-<resource>` 命名。
3. 在 `app/routes.tsx` 注册懒加载路由；属于观测分区的页面同时会出现在 `app/SectionTabs.tsx` 的页签里。
4. 空态与降级：取不到的字段显示「—」，不要补零或编造默认值。

### 提交前检查

```bash
cd frontend && npm run lint && npm run build   # 类型检查 + 构建
uv run python -c "import dba.main"             # 后端可导入
```

### 编码约定

- 金额一律用 `micro_usd` 整数，时间一律 UTC；换算只发生在展示层。
- 外部依赖不可用时**降级而非崩溃**：返回空结构并在 `/ready` 暴露状态。
- 破坏性动作（硬熔断、删除、归档）默认为关闭或需显式确认。
- 前端数值列右对齐并使用等宽数字；时间、时延、成本统一走 `lib/format.ts`。

### 已知限制

- **IM 播报未接真实网关**：`BroadcastAgent` 默认注入日志实现，只落结构化日志。
- **复用率曲线可能为空**：`skill_usage` 目前缺少持续写入方，曲线通常返回空态。
- **取消 Run 仅在单副本生效**：取消令牌是进程内结构，跨副本需要 Redis 广播。
- **Agent 上报需要令牌**：`X-Agent-Token` 必须匹配 `DBA_AGENT_TOKEN`，未配置时接口 fail-closed。
- **CI 只做依赖解析与前端类型检查、构建**，不产出制品。

---

## License

内部项目，未附开源许可证。
