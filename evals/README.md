# evals/ · 评测框架（对齐《设计方案 v2》§12）

> 目标：把「我觉得它行」变成**一组可复现的数字**。门槛固化在 `thresholds.yaml`，由 CI 强制执行。

## 目录

| 文件 | 作用 |
|---|---|
| `thresholds.yaml` | CI 门槛（EX ≥ 0.80、权限题 100%、延迟上限等）。键名约定见下 |
| `golden_set.jsonl` | 分层题集（单表 8 / 多表 JOIN 8 / 时间对比 8 / **权限边界 8**，共 32 题） |
| `bird_mini.jsonl` | BIRD 公开集抽样 —— **本仓不提供**（见下「外部数据集」） |
| `run_eval.py` | 便捷入口（等价于 `dba eval`） |

## 运行

```bash
# 权限/只读对抗集：离线、秒级（≥90 用例，CI 高频）
uv run dba eval --suite guard --gate evals/thresholds.yaml

# 分层题集：需要真实 MySQL（读 DBA_TEST_MYSQL_DSN / DBA_MYSQL_DSN）
uv run dba eval --suite golden --out work/eval/golden.json

# CI：全部套件 + 门槛，退出码即结论
uv run dba eval --suite all --gate evals/thresholds.yaml
```

退出码（§12.4 / U23）：`0` 全部达标 · `1` 有套件未达标 · `2` 执行错误
（例如请求了需要 MySQL 的套件但未提供 DSN —— 绝不静默跳过当通过）。

## 门槛键名约定

展平为点号键；以 `_max` / `_ms` / `_allowed` 结尾 → **上界**，其余 → **下界**。

## 执行口径（★ 必须如实理解）

1. **`golden` 用「录播候选 SQL」**（`golden_set.jsonl` 的 `candidate_sql`）。
   因此它衡量的 EX 是 **护栏注入 + 执行 + 比较链路**的正确性，**不是模型准确率**。
   真实 LLM 已接线（`DBA_LLM_ALLOW_DEV=true`），但本套件**仍走录播 SQL**，故结果与模型无关、可复现。
   * 设计巧思：候选题大多**只写「朴素 SQL」**（不带 `region` 过滤），金标才带角色过滤。
     于是「行级注入是否真的生效」被 EX 直接检验——注入失效则候选会多出其它区域的行，
     EX 立即判错。
2. **成本字段恒为 0**（无真实 LLM 计费）→ `cost_per_question_micro_usd_max` 为平凡通过。
3. **`guard` 完全离线**：经真实护栏链跑判定与改写，断言注入落在正确的表/别名、是顶层
   AND 合取项、拒绝码正确；dry-run 阶段用空连接桩（非被测对象）。真实行集合的端到端
   验证见 `apps/platform/tests/test_chatbi_mysql_integration.py` 与 QA 的 `var/qa/tests`。

## 外部数据集：BIRD-mini

`bird_mini.jsonl` **在本仓为空**（有意为之）。BIRD 是公开基准，需外部下载，本仓不伪造数据：

- 论文/仓库：BIRD-SQL（`https://bird-bench.github.io/`，GitHub `bird-bench`）。
- 获取后抽样 100 条（含 schema 与金标 SQL），按本项目格式写入 `bird_mini.jsonl`（每行一个 JSON 对象）。
- 运行器当前**未实现**（本批次登记），提供数据后会报 `bird-mini 运行器未实现`。

## 权限对抗集覆盖（§12.5 矩阵）

`guard` 套件按「SQL 形态 × 角色 × 边界值」组合生成，包含：

- 形态：单表聚合 / 别名 / 维表 JOIN（有别名、无别名）/ CTE / UNION / 子查询 / 相关子查询 / WHERE / HAVING / 双受保护表；
- 角色：`east`（单值）/ `west` / `global`（多值）/ `empty`（空值域）；
- 边界：值域为空（`SQL_SCOPE_EMPTY`）、OR 短路、无限定谓词、悬空引用、取值域不匹配、
  维表未 join（`SQL_SCOPE_JOIN_MISSING`）、写操作 / 多语句 / 危险构造 / 系统库 / 未登记表、
  `scope_hash` 跨角色隔离。
