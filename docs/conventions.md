# 命名与代码组织约定

> 本文档把散落在各模块里的隐式约定收口成显式标准，供后续开发/评审对齐。
> 与《设计文档 v2》§5.9 归属矩阵、《实现要点清单》§4.0 分层红线配套阅读。

## 1. 分层与归属（不变）

```
L1 api/          接入层（FastAPI 路由 + 中间件 + 依赖注入）
L2 dba_runtime   运行时内核（Pipeline / RunContext / Protocol 契约，独立包）
L3 modules/      领域（chatbi / observability / finops），禁止互相 import（ruff banned-api 强制）
L4 capabilities/ 能力（budget / cost / embedding / memory / messaging / routing /
                 semantics / skills / telemetry）
L5 storage/      存储（es / milvus / minio / mongo / mysql / redis + protocols.py 契约）
```

- L3 之间只经 DI 注入（`build_*` 构造参数），类型可用 Protocol 收紧，禁止 `Any` 泛滥。
- L3/L4 只 import L5 的 `protocols.py`（类型契约），禁止 import 具体仓储实现。

## 2. Repository 方法命名（L5 storage/*/repo.py）

| 语义 | 命名 | 示例 |
|---|---|---|
| 主键 / 自然键单查 | `get(...)` | `AuthUserRepo.get(user_id)`、`BudgetRepo.get(budget_id)` |
| 唯一字段单查 | `by_<field>(...)` | `AuthUserRepo.by_username`、`SemMetricRepo.by_codes`、`SemFieldMappingRepo.by_logical`、`SkillRegistryRepo.by_key` |
| 列表（可带过滤参数） | `list_<复数>(flt)` | `RunRepo.list_runs`、`AppAgentRepo.list_agents`、`AlertEventRepo.list_events` |
| 模糊 / 排序检索 | `search(...)` | `SemMetricRepo.search`、`SkillRegistryRepo.match_intent` |
| 时间窗「生效行」 | `lookup(...)` | `PriceBookRepo.lookup(provider, model, at)` |
| 版本「最新/生效」 | `latest(...)` / `effective(...)` | `DashboardSpecRepo.latest`、`BudgetRepo.effective` |
| 幂等写入 | `upsert(row)` / `insert_if_absent(row)` | `SkillRegistryRepo.upsert`、`BudgetReservationRepo.insert_if_absent` |

> 反例已修正：`BudgetRepo.by_id` → `BudgetRepo.get`（与其它 repo 的 `get` 对齐）。

## 3. 时间与金额约定

- **UTC naive**：库内 `DATETIME(3)` 均为 UTC naive；取当前时间统一用 `dba.util.time.utcnow_naive()`（禁止 `datetime.now()` 裸用 / 各处自造 `_now`）。
- **金额**：一律 `micro_usd` 整数，只做整数求和，禁止浮点累加。
- **时间窗半开**：`[start, end)`，避免整点边界把同一 Run 计两次。
- API 层的 `from/to` 解析统一用 `dba.api.v1._common.parse_window / parse_ts`。

## 4. 决策枚举

- 内核 `dba_runtime.BudgetDecision.decision` / `Reservation.decision`：**大写** `ALLOW/SOFT/HARD(/BLOCK)`。
- L4 `capabilities.budget.ReservationDecision.decision`：**小写** `allow/soft/hard`。

> 两套语义不同（守卫判定 vs 预留判定），**不强行合并**；新增决策字段时先确认归属再选大小写。

## 5. 降级与占位

- 只读服务方法「依赖缺失 → 返回空/默认值」是**诚实降级**，不是 bug；但必须写 `logger.warning` 说明原因。
- 占位实现一律显式命名 `*Placeholder`，`/ready` 如实返回 `ok/degraded/not_ready`。
- 对外端点若仍是空实现，必须在 docstring 标注「TODO / 已知遗留」，禁止假装有数据。

## 6. 提交与验证

- 每个阶段独立提交（`chore` / `refactor` / `perf` / `feat` 前缀）。
- 提交前必须 `ruff check` + `mypy` + `pytest` 全绿；改前端加 `tsc --noEmit`。
