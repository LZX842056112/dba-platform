# 需求文档 · dba-platform

> **文档性质**：反向推导文档，以当前工作区**实际代码**为唯一事实来源。
> **读者**：产品、研发、测试、运维
> **配套文档**：[PRD](./PRD.md) · [代码设计文档](./design.md)

---

## 1. 编号与优先级规则

**编号**：`FR-<域>-<序号>`。域划分与代码中的路由分组一一对应：

| 域 | 范围 | 对应路由/模块 |
|---|---|---|
| `FR-A` | 认证与权限 | `/api/v1/auth/*` |
| `FR-B` | ChatBI 对话与 Run | `/api/v1/chat/*` |
| `FR-C` | 大屏管理 | `/api/v1/dashboards/*` |
| `FR-D` | 语义层 | `/api/v1/semantics/*` |
| `FR-E` | Observability | `/api/v1/obs/*` |
| `FR-F` | FinOps | `/api/v1/finops/*` |
| `FR-G` | Worker 周期任务 | `apps/worker` |
| `FR-H` | 运维端点 | `/health` `/ready` `/metrics` |
| `FR-I` | 前端页面 | `frontend/src/pages/*` |

**优先级**：

- **P0**：缺失即产品不可用或安全红线失效（鉴权、护栏、预算不超发、主链路出屏）。
- **P1**：核心体验与治理能力（自愈、SSE 续传、异常处置、成本归因、数据不悬空）。
- **P2**：增强与运维便利性（导出、日志日报、冷归档、外部上报）。

**鉴权口径**：`PUBLIC`（无需凭证）、`JWT`（Bearer）、`ADMIN`（role 1）、`SUPER`（role 1，演示与 admin 同角色）、`AGENT`（`X-Agent-Token`）、`TICKET`（一次性流票据）。

---

## 2. 功能需求

### 2.1 FR-A 认证与权限（6 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-A-01 | `POST /auth/login`：校验用户名口令，签发 access + refresh token，返回 `expires_in` | P0 | PUBLIC | 口令错误返回 401 且文案不区分「用户不存在/口令错误」；成功响应含 `access_token` / `refresh_token` / `expires_in`（默认 3600s） |
| FR-A-02 | `POST /auth/refresh`：用 refresh token 换新 access token | P0 | PUBLIC | 非 `typ=refresh` 的 token 被拒绝；成功时原 refresh token 继续有效 |
| FR-A-03 | `POST /auth/logout`：无状态登出 | P2 | JWT | 返回 `{"ok": true}`；服务端不做吊销（无状态实现的显式约定） |
| FR-A-04 | `GET /auth/me`：返回用户、角色、业务线与 `scope_summary` | P0 | JWT | `scope_summary` 含 `accessible_tables` / `scope_predicates` / `hash` / `rule_version`；`hash` = `sha256(canonical_json(...))[:32]`，与缓存 `scope_hash` 同口径 |
| FR-A-05 | `GET /auth/users`：用户列表 | P2 | ADMIN | 当前实现返回空列表（仓储层无列表接口，避免直连表）——见 §5 缺口 |
| FR-A-06 | `POST /auth/users/{id}/roles`：授予角色 | P1 | ADMIN | 幂等：重复授予不报错；存储不可用时返回 `503` + `reason=storage_unavailable` |

**边界与依赖**：JWT 为 HS256 标准库实现（`hmac` + `hashlib` + `base64`），不引第三方库；`sub` 即 user_id，`roles` / `biz_line_id` 进 claims，鉴权路径**不访问数据库**。口令校验兼容 `sha256` 十六进制与明文（演示用，生产必须替换为 bcrypt/argon2）。

### 2.2 FR-B ChatBI 对话与 Run（9 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-B-01 | `POST /chat/sessions`：新建会话 | P0 | JWT | 返回 `session_id`（形如 `sess_<24hex>`）；Mongo 不可用时仍返回 id 并记 warning（不阻塞提问） |
| FR-B-02 | `GET /chat/sessions`：当前用户会话列表 | P1 | JWT | 只返回本人会话；`size` 上限 200 |
| FR-B-03 | `DELETE /chat/sessions/{sid}`：软删除 | P1 | JWT | 非本人会话返回 403（错误体含 `code=40300`）；删除后消息列表返回空 |
| FR-B-04 | `GET /chat/sessions/{sid}/messages`：消息列表 | P1 | JWT | 按 `seq` 顺序返回；已删除会话返回空 |
| FR-B-05 | `POST /chat/sessions/{sid}/query`：启动一次 Run | P0 | JWT | 空问题返回 400（`40001`）；立即返回 `trace_id` + `stream_ticket` + `expires_in`，**不直接返回 SSE 流**；ChatBI 未装配（MySQL 不可用）返回 503（`50301`） |
| FR-B-06 | `POST /chat/stream-ticket`：单独签发订阅票据 | P1 | JWT | 缺 `trace_id` 返回 400；票据默认 60s 有效 |
| FR-B-07 | `POST /chat/sessions/{sid}/query/cancel`：取消 Run | P1 | JWT | 通过进程内取消令牌终止流水线——**跨副本不生效**（见 §5 缺口） |
| FR-B-08 | `GET /chat/runs/{trace_id}`：Run 结果快照 | P0 | JWT | 返回大屏 JSON 与状态；SSE 重连耗尽时前端用它降级 |
| FR-B-09 | `GET /chat/sql-audit`：SQL 审计查询 | P1 | JWT | 每条含 `sql_fingerprint`（`sha256(sql)[:64]`）、`decision`（`allow/rewrite/deny/retry`）、`guard_stage`、`deny_reason`、`scope_injected`、`scope_hash`、`rows_returned`、`exec_ms` |

**流水线与护栏（P0 安全红线）**

- 七步固定为 `intent → schema_link → sql_gen → sql_guard → sql_exec → visual → narrator`；`sql_exec` 必须有独立 span、超时与回退语义。
- 护栏顺序固定为 `readonly → dialect → row_scope → row_scope_verify → limit → dry_run`，全部 fail-closed。
- **越权类错误（`SQL_PERMISSION_DENIED`）短路不重试**；仅 `SQL_EXEC_TRANSIENT`（超时/锁等待/连接中断）允许 `goto sql_gen` 重生成。
- 重生成必须携带失败 SQL 与校验反馈，且不丢失可访问表清单。
- `readonly` 必须：请求恰好一条语句、仅只读类型、危险构造黑名单（`INTO OUTFILE/DUMPFILE`、`FOR UPDATE`、`LOCK IN SHARE MODE`、会话变量赋值、`/*! */` 注释注入）、表与函数白名单。
- `row_scope` 必须：区分「未配置规则」（放行）与「规则值域为空」（拒绝 `SQL_SCOPE_EMPTY`）；谓词按**每个 SELECT 自身作用域**注入，而非统一挂最外层 WHERE。
- `limit` 必须：同时施加行数上限与 `MAX_EXECUTION_TIME` 优化器提示；应用侧再叠加 `asyncio.timeout`，形成双保险。
- `dry_run` 只覆盖元数据类错误，**不得**被当作行级安全防线或运行时错误兜底。

**取数与执行**

- 全平台**唯一**真实 SQL 执行入口是 `QueryExecutor.execute`；任何绕过它的取数路径违反红线。
- 结果行数上限默认 5000（超限截断并标记 `truncated`）；执行超时默认 30s。
- 超过内联阈值（默认 500 行）的结果不内联，改走对象存储预签名 URL。

### 2.3 FR-C 大屏管理（4 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-C-01 | `GET /dashboards/{did}`：最新版本大屏 JSON | P0 | JWT | 返回 `DashboardSpec`：必含 `schema_version` / `dashboard_id` / `version` / `title` / `layout` / `panels` / `data_sources` / `meta` |
| FR-C-02 | `GET /dashboards/{did}/versions`：版本列表 | P2 | JWT | 返回历史版本数组（`versions[]` 版本化存储） |
| FR-C-03 | `POST /dashboards/{did}/publish`：发布 | P2 | JWT | 返回 `url` 与 `expires_at`（默认 30 天） |
| FR-C-04 | `POST /dashboards/{did}/export`：导出 | P2 | JWT | `format` 仅接受 `pdf` / `xlsx`，否则 400（`40001`）；返回 `object_key` + 预签名 `presigned_url`（1 小时） |

**数据源约定（硬约定）**

- `schema_version` 必填（前端据此做兼容分支），当前 `"1.0"`。
- `mode="inline"` 内联小结果；`mode="s3"` 时**不内联**，前端直拉预签名 URL。
- `panel.query.scope_hash` 仅用于前端一致性提示，**不是安全防线**。

### 2.4 FR-D 语义层（6 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-D-01 | `GET /semantics/metrics`：口径列表 | P0 | JWT | 支持 `biz_line_id` 与关键词检索；无关键词时以空串检索命中前 N 条（默认 50） |
| FR-D-02 | `GET /semantics/metrics/{code}`：口径详情 | P0 | JWT | 返回单条口径（含版本）；不存在返回 `{}` |
| FR-D-03 | `POST /semantics/metrics`：新建口径 | P1 | ADMIN | 必填 `code` + `metric_name` + `caliber_desc` + `sql_expr`，缺字段返回 400 且 `detail.missing` 列出缺失项 |
| FR-D-04 | `PATCH /semantics/metrics/{code}`：更新口径 | P1 | ADMIN | 版本号 +1；无更新字段返回 400；不存在返回 404 |
| FR-D-05 | `GET /semantics/search`：口径检索 | P1 | JWT | `q` 必填，`top_k` ∈ [1,50]，默认 8 |
| FR-D-06 | `GET /semantics/mappings`：字段映射 | P0 | JWT | 支持按 `logical_field` 过滤；无过滤返回全量映射（供 schema_link 提示词与表白名单兜底） |

### 2.5 FR-E Observability（19 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-E-01 | `GET /obs/overview`：总览 | P0 | JWT | 返回 KPI 与 Top Agent；ChatBI 不可用时返回空壳默认值而非 500 |
| FR-E-02 | `GET /obs/topology`：调用拓扑 | P1 | JWT | 由 `run_doc.spans[]` 父子关系聚合；改一个 Agent 的调用链拓扑应随之变化 |
| FR-E-03 | `GET /obs/agents`：Agent 列表 | P1 | JWT | 支持过滤条件；存储不可用返回 `[]` |
| FR-E-04 | `GET /obs/agents/{uid}`：Agent 详情 | P1 | JWT | 返回注册信息与最近心跳 |
| FR-E-05 | `POST /obs/agents/register`：注册 Agent | P0 | AGENT | 写入 `agent_uid` / `name` / `runtime_type`（`internal_pipeline` / `external_sdk` / `otel_agent`）等 |
| FR-E-06 | `POST /obs/agents/{uid}/heartbeat`：心跳 | P1 | AGENT | 更新 `last_heartbeat_at` |
| FR-E-07 | `GET /obs/runs`：Run 列表 | P0 | JWT | 只按时间窗查询（`run` 为按月分区，仅用 `trace_id` 无法分区裁剪） |
| FR-E-08 | `GET /obs/runs/{trace_id}`：Run 详情 | P0 | JWT | 返回 span 树；读取失败降级为空对象并记 warning |
| FR-E-09 | `GET /obs/self-cost`：平台自身成本 | P1 | JWT | 排除观测/成本自身模块，避免自指 |
| FR-E-10 | `GET /obs/metrics/timeseries`：指标时序 | P0 | JWT | 数据来自 `metric_daily`，窗口为半开区间 `[since, until)` |
| FR-E-11 | `GET /obs/metrics/skills`：技能指标 | P0 | JWT | 返回技能复用率与死技能（近 30 天零调用，或调用 ≥5 次成功率 <20%） |
| FR-E-12 | `GET /obs/metrics/memory`：记忆指标 | P1 | JWT | 返回记忆命中率（命中检索次数 / 总检索次数） |
| FR-E-13 | `GET /obs/skills`：技能清单 | P1 | JWT | 支持按业务线与死技能过滤 |
| FR-E-14 | `GET /obs/anomalies`：异常列表 | P0 | JWT | 支持按 `status` / `category` / `severity` 过滤 |
| FR-E-15 | `GET /obs/anomalies/{id}`：异常详情 | P1 | JWT | MySQL `alert_event`（展示摘要）+ Mongo `anomaly_report`（完整归因）双写合并 |
| FR-E-16 | `POST /obs/anomalies/{id}/ack`：确认告警 | P1 | JWT | 记录确认人与时间；失败向上抛错不吞 |
| FR-E-17 | `POST /obs/anomalies/{id}/resolve`：解决告警 | P1 | JWT | 幂等：重复解决不报错 |
| FR-E-18 | `POST /obs/otel/v1/traces`：OTLP 接入 | P1 | AGENT | 支持 JSON 与 protobuf 两种 Content-Type；非法 JSON 返回 400；自报数据 `usage_source=self_reported` |
| FR-E-19 | `GET /obs/costs/attribution`：四维归因（观测入口） | P1 | JWT | 委托 FinOps 服务；未装配时返回 `{trace_id, breakdown: []}` |

**异常检测需求（P0）**

- 检测必须**无阈值**：EWMA 基线（`α=0.3`）+ MAD 稳健偏差（`k=3.5`），基线与 MAD 只用历史窗口（不含待检点）。
- 必须提供**静默失败**检测（无报错但无产出 / 低于 P5）。
- 检测器不得把自己算进业务：扫描排除 `observability` / `finops` 模块。
- 每条异常必须双写：MySQL 可检索事件行 + Mongo 完整归因报告。

### 2.6 FR-F FinOps（22 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-F-01 | `GET /finops/cost/summary`：成本概览 | P0 | JWT | 返回总量与按模块/业务线/模型分解 |
| FR-F-02 | `GET /finops/cost/timeseries`：成本时序 | P0 | JWT | **必须**经 `ObservabilityService.timeseries` 读取，禁止直连 `metric_daily` |
| FR-F-03 | `GET /finops/cost/attribution`：四维归因 | P0 | JWT | 维度为业务线 / Agent / 模型 / 技能 |
| FR-F-04 | `GET /finops/cost/top-spenders`：TopN 花钱方 | P1 | JWT | 降序返回，支持窗口参数 |
| FR-F-05 | `GET /finops/cache/stats`：缓存统计 | P1 | JWT | 返回缓存命中相关计数 |
| FR-F-06 | `GET /finops/cost/coverage`：计价覆盖率 | P0 | JWT | 返回 `priced_ratio`；未计价明细不得按 0 静默计入 |
| FR-F-07 | `GET /finops/curve/reuse-vs-token`：复用率-成本曲线 | P1 | JWT | 必须同时返回 **命中组与对照组**（`hit` / `miss`）与相关系数；数据缺失时诚实呈现空态 |
| FR-F-08 | `GET /finops/budgets`：预算列表 | P1 | ADMIN | 返回启用中的有效预算 |
| FR-F-09 | `POST /finops/budgets`：新建/更新预算 | P1 | ADMIN | 支持 `scope_type`（GLOBAL/BIZ_LINE/AGENT/TEAM）、`period`（DAY/WEEK/MONTH）、`soft_limit_pct` / `hard_limit_pct` / `soft_action` / `hard_action` |
| FR-F-10 | `PATCH /finops/budgets/{id}`：调整预算 | P1 | ADMIN | 部分更新；`hard_limit_pct` 默认 90，100 视为禁用 HARD 档 |
| FR-F-11 | `GET /finops/budgets/{id}/usage`：用量查询 | P1 | ADMIN | 返回已消耗、已预留与限额 |
| FR-F-12 | `GET /finops/prices`：价格表查询 | P1 | ADMIN | 支持按 provider / model 过滤 |
| FR-F-13 | `POST /finops/prices`：价格表写入 | P1 | ADMIN | 字段含 `billing_unit`（PER_1K_TOKEN / PER_CALL / PER_SECOND）与输入/输出/缓存读写单价 |
| FR-F-14 | `POST /finops/prices/sync`：价格同步 | P2 | ADMIN | 外价格源同步；失败不得污染现有价格 |
| FR-F-15 | `POST /finops/cost/recompute`：价格修正后重算 | P0 | ADMIN | 因明细固化的是 token 数与 `price_book_id`，重算结果必须**精确** |
| FR-F-16 | `GET /finops/recommendations`：优化建议列表 | P1 | JWT | 建议只读；含依据信号（成本归因 / 缓存命中 / 死技能） |
| FR-F-17 | `POST /finops/recommendations/{id}/apply`：采纳建议 | P1 | ADMIN | 必须显式确认；建议**不自动执行** |
| FR-F-18 | `POST /finops/recommendations/{id}/reject`：拒绝建议 | P2 | ADMIN | 状态置为拒绝，重复操作幂等 |
| FR-F-19 | `GET /finops/guardrail/policy`：读取护栏策略 | P1 | ADMIN | 返回降级/压缩/限流/熔断开关、豁免线、熔断窗口与冷却、`kill_switch` |
| FR-F-20 | `PATCH /finops/guardrail/policy`：修改护栏策略 | P0 | SUPER | 硬熔断默认关闭；任何激进动作必须多重条件成立 |
| FR-F-21 | `POST /finops/guardrail/kill-switch`：全局逃生开关 | P0 | SUPER | 置位后所有预算守卫动作停止（最后一道保险） |
| FR-F-22 | `GET /finops/loop-alerts`：死循环告警 | P1 | JWT | 同 Agent 同工具同参数的滑动窗口（默认 300s）计数 ≥ 8 触发 |

**预算守卫需求（P0）**

- 三段式：LLM 调用前 `reserve`，调用后 `settle`（按实际成本结算并释放差额）或 `release`。
- `reserve` 必须在 Redis 用 Lua 原子执行「判断 `consumed + reserved + amount` 与上限」再 `HINCRBY`，否则并发会超卖。
- MySQL `budget_reservation` 为权威账本；`settle` / `release` **必须幂等**，状态机 `RESERVED → SETTLED | RELEASED | EXPIRED`，非法迁移不得改数。
- Redis 不可用时按 `guardrail_degrade_policy` 决定 `fail_open_mysql`（走 MySQL 行锁兜底）或 `fail_closed`。
- `self_reported` 用量不计入预算。
- 默认行为：软限告警不阻断；硬熔断默认关闭；存在 `exemption_priority` 豁免线保护关键业务。

### 2.7 FR-G Worker 周期任务（8 项）

| ID | 需求 | 优先级 | 触发（UTC） | 验收标准 |
|---|---|---|---|---|
| FR-G-01 | `rollup`：Run 明细 → `metric_daily` | P0 | 每日 02:00 | 幂等：全量重算 + 主键 upsert，重跑不翻倍；`avg_latency_ms` 按 count 加权 |
| FR-G-02 | `partition`：分区维护 | P1 | 每月 1 日 01:00 | `run` / `metric_daily` 预建未来 3 个月分区；幂等 |
| FR-G-03 | `anomaly_scan`：异常扫描 | P0 | 每 15 分钟 | 触发无阈值检测 + 静默失败检测；不读任何阈值配置 |
| FR-G-04 | `report`：日报快照 | P2 | 每日 07:00 | 落对象存储 `reports/daily/{date}.json`；存储不可用时返回 `persisted=False`，**不生成假对象 Key** |
| FR-G-05 | `reconcile`：三方对账 | P0 | 每日 03:00 | Redis ↔ MySQL ↔ 明细累加相对误差 ≤ 1%；超差写告警 |
| FR-G-06 | `stale_run`：滞留 Run 收口 | P1 | 每 10 分钟 | 超过 15 分钟仍 `running` → 置 `timeout` 终态 |
| FR-G-07 | `semcache_gc`：语义缓存 GC | P2 | 每 30 分钟 | 删除 `expire_at < now` 的条目；幂等可重试 |
| FR-G-08 | `archive`：冷归档 | P2 | 每周日 04:00 | 超过 90 天的 Run 导出 JSONL 落对象存储 |

**共性要求（P0）**：每个 job 必须① 被 `RunContext(module="system")` 包裹，② 启动前断言 `bind_metering` 已生效，③ 有 Redis 分布式锁（`job:lock:{id}`，TTL 600s）保证多副本同时刻只执行一次；无 Redis 时**不静默假设安全**，必须告警。

### 2.8 FR-H 运维端点（3 项）

| ID | 需求 | 优先级 | 鉴权 | 验收标准 |
|---|---|---|---|---|
| FR-H-01 | `GET /health`：存活探针 | P0 | PUBLIC | 不检查任何依赖，恒返回 `{"status":"ok"}`；Redis 故障时也**必须** 200 |
| FR-H-02 | `GET /ready`：就绪探针 | P0 | PUBLIC | 逐项返回组件可达性；仅当 mysql/mongodb/redis **全部**不可用时 503（`not_ready`）；任一其他组件不可用返回 200 + `degraded`；必须包含 `metering_bound` |
| FR-H-03 | `GET /metrics`：自监控指标 | P1 | PUBLIC | Prometheus 文本格式，至少含 `telemetry_dropped_total`、`telemetry_queue_depth`、`dba_metering_bound`、`dba_build_info` |

**探针路径不得产生 `run.started`、不限流、不审计**——健康检查不是业务 Run。

### 2.9 FR-I 前端页面（11 项）

| ID | 页面 | 路由 | 优先级 | 验收标准 |
|---|---|---|---|---|
| FR-I-01 | 登录 | `/login` | P0 | 用户名 + 口令换 JWT 并跳转对话页；错误显示提示文案 |
| FR-I-02 | 对话 | `/` | P0 | 输入问题 → 显示七步进度 → 逐面板出图 → 流式结论；SSE 断线自动重连，重连耗尽后降级为一次性拉取终态 |
| FR-I-03 | 观测总览 | `/observability` | P0 | 四问 KPI 卡 + Top Agent + 平台自身成本，15s/30s 轮询 |
| FR-I-04 | 调用拓扑 | `/observability/topology` | P1 | 图形式呈现 Agent↔工具关系 |
| FR-I-05 | Runs | `/observability/runs` | P0 | 列表 10s 轮询，可跳转详情 |
| FR-I-06 | Run 详情 | `/observability/runs/:traceId` | P0 | 展示 span 列表，自愈重试痕迹可见 |
| FR-I-07 | 异常 | `/observability/anomalies` | P0 | 15s 轮询；可确认/解决 |
| FR-I-08 | 成本总览 | `/finops` | P0 | 总量 + 模型分解环图 + TopN 花钱方 + 计价覆盖率 |
| FR-I-09 | 复用率曲线 | `/finops/reuse` | P1 | 折线图 + 节省金额摘要 |
| FR-I-10 | 预算与护栏 | `/finops/budgets` | P1 | 预算列表 + 护栏策略开关（admin 可改） |
| FR-I-11 | 优化建议 | `/finops/recommendations` | P1 | 建议列表 + 采纳/拒绝 + 死循环告警 |

**前端共性要求**：除登录页外全部路由级懒加载；受保护路由由 `RequireAuth` 包裹；所有请求经统一 `apiFetch`（解析错误体、透传 `trace_id`、支持 `X-Trace-Id`）。

---

## 3. 用户场景（端到端）

### 场景 1 · 主链路：问一句出屏（P0）

1. 分析师登录并新建会话。
2. 提问「近 30 天各省 GMV 分布」→ 后端立即返回 `trace_id` 与 `stream_ticket`。
3. 前端用 ticket 建 SSE 连接，依次收到 `run.started`、`agent.step.*`、`sql.generated`、`sql.executing`、`sql.executed`、`dashboard.spec.delta`、`dashboard.spec.ready`、`narration.delta`、`run.finished`。
4. 若护栏拒绝 → 收到 `sql.validation.failed`，流水线回退 `sql_gen` 重生成，成功时收到 `sql.retry.resolved` 与 `agent.step.retrying`。
5. 若越权 → 短路终止，`run.error` 携带 `SQL_PERMISSION_DENIED`，**不重试**。
6. 全过程埋点写入 outbox 并异步投递到 MySQL / Mongo / ES；同一 `trace_id` 可在观测与 FinOps 页面查到。

### 场景 2 · 权限边界（P0）

1. 角色配置了 `row_scope_rule`（表 + 列 + 值域）。
2. `schema_link` 把可访问表与权限值域写进 system prompt。
3. `row_scope` 在 AST 层按 SELECT 作用域注入 `IN` 谓词，`row_scope_verify` 断言顶层 AND 链含该合取项。
4. 规则值域为空 → 返回 `SQL_SCOPE_EMPTY`（50015）拒绝，**不得**退化为放行整表。
5. `sql_audit` 记录 `scope_injected=true` 与 `scope_hash`；`/auth/me` 的 `scope_summary.hash` 与之一致。

### 场景 3 · 异常发现与处置（P1）

worker 每 15 分钟扫描 → EWMA+MAD 判定偏离 → 同批产出静默失败告警 → 双写 `alert_event` + `anomaly_report` → 前端异常页确认/解决。

### 场景 4 · 成本治理闭环（P0）

计量 → 覆盖率检查 → 四维归因 → 预算守卫拦截 → 优化建议 → 价格修正后重算 → 三方对账确认无漂移。

### 场景 5 · 外部 Agent 接入（P2）

注册 `agent_uid` → 周期心跳 → OTLP 上报 span / token → 在观测与成本视图中与内部 Run 一并呈现（`usage_source=self_reported` 不计入预算）。

---

## 4. 非功能需求

| 类别 | 需求 | 验收标准 |
|---|---|---|
| 可用性 | 可选组件缺失不得导致启动失败 | `mysql` 之外任一组件缺失，`build_container` 记为不可用并继续；`di.py` 中每个组件独立 try |
| 降级语义 | 降级必须显式可见 | `/ready` 区分 `ok` / `degraded` / `not_ready`；响应体含 `metering_bound` |
| 可观测 | 埋点必须真正生效 | 启动未绑定 metering 时**拒绝启动**（`RuntimeError`）；`/metrics` 暴露 `dba_metering_bound` |
| 可靠性 | 埋点不丢 | 埋点与 outbox 同事务落库；后台协程投递；重试上限 `outbox_max_attempts=5`，超限落 DLQ |
| 幂等 | 关键写操作可重复执行 | `migrate` / `seed` / rollup / 告警 resolve / 角色授予 / 预算结算均幂等 |
| 安全 | 最小权限 | 业务查询使用只读账号；写操作一律拒绝；JWT 鉴权不查库；越权 fail-closed |
| 一致性 | 时间与金额口径统一 | UTC naive `DATETIME(3)`；金额 `micro_usd` 整数；时间窗半开 `[start, end)` |
| 性能 | 高写入路径不受外键拖累 | `run` / `llm_call` / `tool_call` 只留 `trace_id` 软关联，不建外键 |
| 可维护性 | 单一来源 | 护栏共享 AST 工具模块；worker 不重复实现聚合逻辑；mock/占位实现显式命名 `*Placeholder` |

---

## 5. 已实现 / 未实现 / 降级项

> 以下条目均来自代码中的显式标注，用于避免「看起来已实现」的误判。

### 5.1 已实现

鉴权与角色、ChatBI 七步流水线与五道护栏、`QueryExecutor` 唯一执行入口、大屏 JSON 组装与版本化、SSE 进度与断线续传（含缺口提示）、语义层 CRUD 与检索、观测四角色流水线与无阈值异常检测、三条价值指标、rollup、拓扑、OTLP 接入、告警 ack/resolve、FinOps 四角色、成本归因与重算、预算三段式与护栏策略、死循环检测、worker 8 定时任务、运维三端点、前端 11 页。

### 5.2 未实现或部分实现

| 项 | 现状 | 影响 |
|---|---|---|
| IM 播报网关 | 仅有 `LogBroadcastGateway`（结构化日志），未接飞书/钉钉 webhook | 异常播报不会真正到达 IM |
| 复用率曲线数据源 | `curve_source` 依赖 `skill_usage`，当前无生产写入方，接口通常返回空态 | 曲线图无数据；对照组机制已就绪 |
| `GET /auth/users` | 返回空列表（仓储层无列表接口） | 用户管理页无法列出用户 |
| 取消 Run | 进程内取消令牌表 | 多副本部署下取消只在处理该 Run 的副本生效 |
| Agent 上报鉴权 | `X-Agent-Token` 仅校验非空，无签名/白名单校验 | 外部上报接口鉴权强度不足 |
| 密码存储 | 兼容明文与 `sha256` 十六进制 | 生产必须替换为 bcrypt/argon2 |
| Run 失败 `error_code` | 已知历史问题：失败 Run 的 `error_code` 可能为空 | 失败原因需从 span / 日志定位 |

### 5.3 降级项（设计内，非缺陷）

| 项 | 触发条件 | 降级行为 |
|---|---|---|
| MySQL 不可用 | 驱动缺失或连接失败 | ChatBI / Observability / FinOps 均不装配；`metering` 退化为 `PlaceholderMeteringService`（仅内存/日志，不落库） |
| Redis 不可用 | 连接失败 | 进度写入退化为 `InMemoryProgressWriter`（不跨副本回放）；限流/幂等置空；预算走 MySQL 行锁兜底（`fail_open_mysql`）或直接拒绝（`fail_closed`） |
| ES 不可用 | 连接失败 | 审计落点退化为 `InMemoryAuditSink` |
| Milvus 不可用 | 连接失败 | 向量召回降级，语义检索走结构化路径 |
| MinIO 不可用 | 连接失败 | 大结果无法外置、导出/日报标记 `persisted=False`，不生成假 Key |
| 无 LLM API Key / `env=test` / dev 未放行 | 三态开关 | 使用 `DemoLLMClient` 确定性替身（**不是真实模型**，成本曲线与结论无业务意义） |
| `embedding_backend=local` | 使用进程内模型 | `/ready` **不探测** embedding 外部端点（避免误报 degraded） |
| 价格表缺失 | `price_book` 无匹配 | `CostNormalizer` 返回 `unknown`，**不静默按 0 计** |

---

## 6. 优先级汇总

| 优先级 | 需求条目 | 说明 |
|---|---|---|
| P0 | FR-A-01/02/04、FR-B-01/05/08、FR-C-01、FR-D-01/02/06、FR-E-01/07/08/10/11/14、FR-F-01/02/03/06/15/20/21、FR-G-01/03/05、FR-H-01/02、FR-I-01/02/03/05/06/07/08 | 缺失即不可用或安全红线失效 |
| P1 | FR-A-06、FR-B-02/03/04/06/07/09、FR-C-01 之外、FR-D-03/04/05、FR-E-02~06/09/12/13/15~19、FR-F-04/05/07~14/16/17/19/22、FR-G-02/06、FR-H-03、FR-I-04/09/10/11 | 核心体验与治理 |
| P2 | FR-A-03/05、FR-C-02/03/04、FR-F-18、FR-G-04/07/08 | 增强与运维便利 |

---

## 7. 代码位置

| 主题 | 源码位置 |
|---|---|
| 认证与 JWT | [auth.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/api/v1/auth.py:51) · [deps.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/api/deps.py:1) |
| ChatBI 接口 | [chatbi.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/api/v1/chatbi.py:171) |
| 护栏实现 | [guard/readonly.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/chatbi/guard/readonly.py:1) · [guard/row_scope.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/chatbi/guard/row_scope.py:1) |
| 唯一执行入口 | [executor.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/chatbi/executor.py:1) |
| 大屏 schema | [schemas/dashboard.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/schemas/dashboard.py:1) |
| 观测接口 / 检测器 | [observability.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/api/v1/observability.py:397) · [anomaly.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/observability/anomaly.py:230) |
| FinOps 接口 / 护栏 | [finops.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/api/v1/finops.py:382) · [guardrail.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/modules/finops/guardrail.py:1) |
| 预算服务 | [budget/service.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/capabilities/budget/service.py:249) |
| Worker 任务表 | [scheduler.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/worker/src/dba_worker/scheduler.py:48) |
| 运维端点 | [main.py](D:/Projects/vibeC/workbuddy/dba-platform/apps/platform/src/dba/main.py:160) |
| 前端路由 | [routes.tsx](D:/Projects/vibeC/workbuddy/dba-platform/frontend/src/app/routes.tsx:45) |
