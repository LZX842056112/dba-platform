# 产品需求文档（PRD）· dba-platform

> **文档性质**：反向推导文档。本文以当前工作区**实际代码**为唯一事实来源，由源码结构、常量与注释还原产品意图，不引用任何已删除的测试、评测或历史报告。
> **版本**：v1.0（与代码 `0.1.0` 对齐）　**状态**：当前实现快照
> **读者**：产品/项目负责人、业务分析师、平台工程师、FinOps 负责人、Agent 开发者
> **配套文档**：[需求文档](./requirements.md) · [代码设计文档](./design.md)

---

## 1. 产品定位

**一句话**：`dba-platform` 是一个「**问一句 → 出一屏**」的数据大屏平台——用自然语言生成可直接观看的数据大屏，并同时对「生成过程本身」做治理。

与常规 BI 的差别不在「能不能出图」，而在下面三件事，它们贯穿全部代码：

| # | 差异化设计 | 具体含义 | 代码中的体现 |
|---|---|---|---|
| 1 | **一个 `trace_id` 三视角** | 一次提问（Run）贯穿全程：ChatBI 看它是执行记录、Observability 看它是观测对象、FinOps 看它是计量对象；埋点只做一次，三个模块是同一份运行时数据的三个视角 | `dba_runtime.RunContext` + `run` / `llm_call` / `tool_call` / `run_doc` 统一以 `trace_id` 软关联 |
| 2 | **行级权限生成期下沉** | 权限在 SQL **生成阶段**就注入 `WHERE` 条件，不做「查完再过滤」——否则敏感数据已经进过内存 / 日志 / LLM 上下文 | `row_scope` 护栏（AST 注入）+ `row_scope_verify` 覆盖性断言，fail-closed |
| 3 | **预算守卫前置** | LLM 调用**前**执行 `reserve → settle/release` 三段式，软限降级、硬限熔断；**硬熔断默认关闭**（唯一会中断业务的动作，避免误配全熔断） | `BudgetService` + Redis Lua 原子脚本 + MySQL `budget_reservation` 账本 |

产品不是一个「BI 工具」，而是**给 Agent 舰队用的治理底座**：ChatBI 是被观测/被计量的一等公民业务，Observability 与 FinOps 是围绕它的两条治理主线。

---

## 2. 目标用户

| 角色 | 核心诉求 | 主要入口 | 典型操作 |
|---|---|---|---|
| **业务分析师 / 运营** | 不会写 SQL，但要按口径拿到可信的销售/经营大屏 | 前端「对话」页 | 自然语言提问 → 看七步进度 → 看逐面板出图与结论解读 |
| **数据 / 平台工程师** | 一次失败或变慢的 Run 要能定位到具体 span；SQL 越权要能被拦截并留痕 | 观测 5 页 + `/api/v1/obs/*` + `sql_audit` | 看 Runs 列表 / Run 详情 span 树 / 拓扑 / 异常确认 |
| **FinOps / 成本负责人** | 钱花在哪、能不能省、超预算时系统怎么退让 | FinOps 4 页 + `/api/v1/finops/*` | 成本总览 / 四维归因 / 预算与护栏策略 / 复用率曲线 / 优化建议 |
| **Agent 开发者** | 自己写的 Agent 能被注册、被观测、被计量 | `/obs/agents/register`、`/obs/agents/{uid}/heartbeat` | 注册 `agent_uid`、上报心跳、查看自己的 Run 与成本 |
| **外部 Agent 接入方** | 平台外的 Agent（`external_sdk` / `otel_agent`）也能进同一套视图 | `POST /obs/otel/v1/traces` | 用 OTLP/HTTP 自报 span 与 token 用量 |

登录方式为用户名 + 口令换 JWT；权限模型由「角色 → 行级规则（`row_scope_rule`）」决定，用户可访问的表与谓词集合通过 `/auth/me` 的 `scope_summary` 显式暴露。

---

## 3. 核心功能

产品由三个模块 + 一组公共能力构成。

### 3.1 ChatBI（模块 01）—— 「问一句，出一屏」

- **七步流水线**：`intent → schema_link → sql_gen → sql_guard → sql_exec → visual → narrator`，其中 `sql_exec` 是真实取数步骤（有独立 span、超时与回退语义）。
- **五道 SQL 护栏**（fail-closed，顺序固定）：`readonly → dialect → row_scope → row_scope_verify → limit → dry_run`。
- **自愈回退**：护栏/执行失败按错误分类决定「带反馈重新生成 SQL」还是「直接短路」；越权类错误**不重试**（避免刷 token）。
- **大屏 JSON**：由 `DashboardSpecBuilder` 组装，支持「技能模板确定性路径」与「LLM 生成 JSON 路径」两条来源；超过内联阈值（默认 500 行）的结果不放应用内存，改走对象存储预签名 URL。
- **实时进度**：提问立即返回 `trace_id` + 一次性 `stream_ticket`，前端用 SSE 订阅 21 类命名事件（含心跳与缺口提示），看到逐面板出图与流式结论文本。
- **会话与历史**：会话 / 消息存 MongoDB，消息 `seq` 用原子自增分配，避免并发写撞唯一键。

### 3.2 Observability（模块 03）—— 观测 Agent 舰队自身

- **四角色流水线**：`collect → aggregate → anomaly → broadcast`（线性批处理，每步失败原地重试 1 次后继续，不让旁路治理拖垮主链路）。
- **无阈值异常检测**：EWMA 基线 + MAD 稳健偏差（`α=0.3`、`k=3.5`），不依赖人工维护阈值；同时提供**静默失败**检测（无报错但无产出）。
- **三条价值指标**：技能复用率、死技能、记忆命中率——回答「运行时是否真的在进化」。
- **指标 rollup**：按 `(stat_date, biz_line_id, agent_uid, model)` 全量重算 + 主键 upsert，重跑不翻倍。
- **Agent 拓扑**：从 span 父子关系聚合出「Agent → 工具 / 子 Agent」调用图。
- **OTLP 接入**：平台外 Agent 通过 `POST /obs/otel/v1/traces` 自报，与内部 Run 用同一套结构呈现。
- **异常处置**：告警可 `ack` / `resolve`，MySQL 存可检索事件行，Mongo 存完整归因报告。

### 3.3 FinOps（模块 10）—— Agent 成本治理

- **四角色流水线**：`meter → attribute → guard → optimize`。
- **成本作为派生量**：明细里固化的是 **token 数与 `price_book_id`**，而不是最终金额——所以价格表修正后可以**精确重算历史成本**。
- **四维归因**：业务线 / Agent / 模型 / 技能。
- **预算守卫三段式**：`reserve → settle | release`，Redis Lua 原子执行 + MySQL 权威账本，`settle`/`release` 幂等（非法状态迁移不改数）。
- **护栏动作分级**：默认只告警；可开降级模型、压缩上下文、限流；**硬熔断默认关闭**，并提供豁免线（`exemption_priority`）与全局逃生开关（`kill_switch`）。
- **死循环检测**：同一 Agent 相同工具 + 相同参数的滑动窗口计数（默认 300s / 8 次）触发告警。
- **复用率-成本曲线**：带对照组（命中 / 未命中技能），用数据证明「技能复用率 ↑ → 重复任务 token ↓」。
- **优化建议**：只落库、不自动执行；`apply` 必须经管理端显式确认。

### 3.4 公共能力与界面

- **认证与权限**：HS256 JWT（标准库实现）、角色校验、行级规则编译为 `scope`。
- **语义层**：指标口径（`sem_metric`）、逻辑字段 → 物理列映射（`sem_field_mapping`）、口径词典（`sem_dict_entry`）与检索。
- **大屏管理**：读取最新版本、版本列表、发布、导出（返回预签名 URL）。
- **前端 11 页**：登录 1 页 + 对话 1 页 + 观测 5 页 + FinOps 4 页。

| 页面 | 路由 | 提供的能力 |
|---|---|---|
| 登录 | `/login` | 用户名 + 口令换 JWT |
| 对话 | `/` | 提问、七步进度、自愈提示、SQL 折叠、逐面板出图、流式结论 |
| 观测总览 | `/observability` | 四问 KPI 卡、Top Agent、平台自身成本 |
| 调用拓扑 | `/observability/topology` | Agent ↔ 工具拓扑图 |
| Runs | `/observability/runs` | Run 列表 |
| Run 详情 | `/observability/runs/:traceId` | span 列表（自愈痕迹可见） |
| 异常 | `/observability/anomalies` | 异常列表 + 确认 / 解决 |
| 成本总览 | `/finops` | 总量、模型分解环图、TopN 花钱方、计价覆盖率 |
| 复用率曲线 | `/finops/reuse` | 复用率 vs token 曲线 |
| 预算与护栏 | `/finops/budgets` | 预算列表 + 护栏策略开关（admin） |
| 优化建议 | `/finops/recommendations` | 建议列表 + 采纳 / 拒绝 + 死循环告警 |

---

## 4. 业务目标与成功指标

指标全部取自代码中真实存在、可计算的口径（常量见「代码位置」）：

| 目标 | 指标口径 | 目标值 / 默认阈值 | 数据来源 |
|---|---|---|---|
| 出屏结果可信 | 每次 Run 必须显式报口径（如「GMV 为支付口径、不含税」）、被截断必须说明 | 100% 的解说包含口径声明 | `narrator` Agent |
| 越权不可发生 | 行级谓词注入 + 顶层 AND 链覆盖断言，fail-closed | 覆盖率 100%；值域为空 → 拒绝 | `row_scope` / `row_scope_verify` |
| 查询可控 | 单次查询行数上限 / SQL 执行超时 | 5000 行 / 30s（SQL 侧 `MAX_EXECUTION_TIME` + 应用侧 `asyncio.timeout` 双保险） | `chatbi_max_rows` / `chatbi_sql_timeout_s` |
| 大结果不压内存 | 超过内联阈值转对象存储预签名 URL | 500 行 | `chatbi_inline_row_threshold` |
| 沉淀是真的 | 技能复用率 ↑、死技能可识别 | 复用率为核心观测指标；死技能 = 近 30 天零调用或调用 ≥5 次成功率 <20% | `observability/metrics.py` |
| 成本可信 | 计价覆盖率 `priced_ratio`（有价格明细占比） | 作为成本结论的前置证据 | `finops/agents/meter.py` |
| 账本不漂移 | 三方对账（Redis ↔ MySQL ↔ 明细累加）相对误差 | ≤ 1% | `reconcile.DEFAULT_TOLERANCE` |
| 成本可回放 | 价格表修正后可精确重算历史成本 | 重算而非打补丁 | `finops/agents/attribute.py` |
| 预算不超发 | 软限 80% / 硬限 90%；预留-结算-释放幂等 | 超软限告警/降级；超硬限阻断（可关） | `budget.soft_limit_pct` / `hard_limit_pct` |
| 跑飞的 Agent 能被止血 | 相同工具 + 相同参数的滑动窗口计数 | 300s 内 ≥ 8 次触发死循环告警 | `loop_detector` |
| 状态不悬空 | 滞留 Run 收口 | 超过 15 分钟仍 `running` → 置终态 | `stale_run.STALE_THRESHOLD_MINUTES` |
| 存储不无限膨胀 | Run 冷归档保留期 / 分区预建 | 90 天；未来 3 个月分区 | `archive` / `partition` |
| 进度不丢不重 | SSE 回放窗口 + 缺口显式提示 | 最近 200 条、TTL 1h、ticket 60s | `run:progress` / `stream_tickets` |

---

## 5. 关键用户旅程

**旅程 1 · 业务分析师：问一句出屏**
登录 → 新建会话 → 输入「近 30 天各省 GMV 分布」→ 立即拿到 `trace_id`，页面显示七步进度 → 护栏通过、真实取数、逐面板渲染 → 底部流式给出结论与口径说明；若 SQL 被护栏拦下，界面显示自愈重试提示，重试成功后出图。

**旅程 2 · 平台工程师：定位一次失败 Run**
从 Runs 列表进入 Run 详情 → 查看 span 树与重试标记 → 若为越权拒绝，从 `sql_audit` 查到 `guard_stage`、`deny_reason` 与 `scope_hash`；若为观测缺口，从异常页确认 / 解决告警。

**旅程 3 · FinOps 负责人：发现成本异常并处置**
成本总览看到某业务线成本突增 → 四维归因定位到模型或技能 → 查看优化建议（如命中率低的技能）→ 在预算页调整软/硬限与护栏开关 → 硬熔断仍默认关闭，必要时用 `kill_switch` 全局逃生。

**旅程 4 · 外部 Agent 接入**
调用注册接口登记 `agent_uid` 与 `runtime_type=otel_agent` → 周期性上报心跳 → 通过 OTLP 端点自报 span 与 token（`usage_source=self_reported`）→ 其 Run 与内部 Run 在同一套观测/成本视图里出现。

---

## 6. 范围与非目标

**范围（当前已实现）**

- 只读数据分析链路（SELECT 语义），护栏对写操作一律拒绝。
- 大屏由「技能模板 + LLM 生成」产出，前端负责渲染与交互。
- 平台自观测与自计量（含外部 Agent 的 OTLP 接入）。

**非目标（代码中明确未做或不承诺）**

| 非目标 | 说明 |
|---|---|
| 拖拽式 BI 设计器 | 大屏来自生成结果，不提供手工拖拽画布 |
| 数据写入 / ETL | 平台不写业务库；`fact_sales` 等演示数据由 `scripts/seed.py` 生成 |
| 演示替身不等于生产能力 | 无 API Key 或未放行时走 `DemoLLMClient`，成本曲线与结论无业务意义 |
| 多租户隔离 | 当前是单租户 + 业务线/角色维度 |

---

## 7. 约束与假设

- **外部组件集中部署**：MySQL / MongoDB / Redis / Milvus / Elasticsearch / MinIO / Embedding 部署在虚拟机 `192.168.200.10`，本机不拉容器。
- **时间与金额口径**：全链路 UTC（库内 `DATETIME(3)` 为 UTC naive），金额一律 `micro_usd` 整数（`1 USD = 1_000_000 micro_usd`），禁止浮点累加。
- **硬依赖与可降级**：`mysql` / `mongodb` / `redis` 为硬依赖；`milvus` / `elasticsearch` / `minio` / `embedding` 可降级，降级必须如实反映在 `/ready`，绝不假装可用。
- **模型可替换**：只依赖 OpenAI 兼容协议，换 Qwen / DeepSeek / GLM 时上层零改动。
- **可降级但不静默**：所有降级路径都写 warning 日志并在响应中体现（例如播报网关缺失时返回日志网关语义）。

---

## 8. 术语表

| 术语 | 含义 |
|---|---|
| Run | 一次完整的业务执行，以 `trace_id` 标识 |
| Span | Run 内的一个执行片段，记录 `span_id` / `parent_span_id` / 耗时 / 状态 |
| 护栏（Guard） | SQL 安全校验链的一道关卡，任意一道 fail-closed 即拒绝 |
| `scope_hash` | 由「可访问表 + 谓词 + 规则版本」派生的指纹，用于缓存一致性与前端提示（**不是安全防线**） |
| `micro_usd` | 金额的最小单位整数（1 USD = 1e6） |
| 技能（Skill） | 可复用的问法/取数模板，命中即走确定性路径，省 token |
| 软限 / 硬限 | 预算达 `soft_limit_pct` 触发告警或降级；达 `hard_limit_pct` 触发阻断（默认关闭熔断） |
| DLQ | 投递失败记录的本地落盘队列（`var/dlq/telemetry.jsonl`） |
| outbox | 与业务同事务落库、由后台协程异步投递的可靠事件表 |

---

## 9. 代码位置

| 主题 | 源码位置 |
|---|---|
| 产品入口 / 装配 | [main.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/main.py:1) |
| 配置与默认值 | [config.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/config.py:1) |
| 依赖装配与降级 | [di.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/di.py:1) |
| 七步流水线定义 | [pipeline.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/chatbi/pipeline.py:30) |
| 五道护栏顺序 | [guard/__init__.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/chatbi/guard/__init__.py:5) |
| 观测四角色流水线 | [pipeline.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/observability/pipeline.py:21) |
| 无阈值异常检测常量 | [anomaly.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/observability/anomaly.py:230) |
| FinOps 四角色流水线 | [pipeline.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/finops/pipeline.py:17) |
| 预算三段式与限额 | [service.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/capabilities/budget/service.py:249) |
| 死循环检测默认值 | [loop_detector.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/finops/loop_detector.py:99) |
| Worker 周期任务表 | [scheduler.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/worker/src/dba_worker/scheduler.py:48) |
| 前端路由与页面 | [routes.tsx](D:/Projects/vibeC/workbuddy/dba-platform/frontend/src/app/routes.tsx:45) |
| 演示数据与口径种子 | [seed.py](D:/Projects/vibeC/workbuddy/dba-platform/scripts/seed.py:1) |
