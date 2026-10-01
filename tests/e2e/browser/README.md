# 真实浏览器全流程联调 e2e

用 **真实 Chrome**（`chrome-ws` 驱动 CDP）跑通 `登录 → 提问 → 七步流水线 → SSE 进度 → SQL 折叠 → 逐面板出图`，
并断言页面上的真实 DOM 状态。

> 与 `tests/e2e/test_e2e_chatbi_smoke.py` 的分工：那个是**函数级**（替身注入、不起服务、不用浏览器）；
> 本套是**端到端**（真 HTTP + 真 SSE + 真 Chrome + 真数据库）。

## 前置条件

| 项 | 要求 |
|---|---|
| 外部组件 | 虚拟机 `192.168.200.10` 的 MySQL / MongoDB / Redis 可达（`.env` 指向它） |
| 数据 | 已执行 `uv run dba migrate` + `uv run dba bootstrap-storage` + `uv run dba seed --demo` |
| 依赖 | `uv sync --all-packages --extra prod`（含 `asyncmy`/`motor` 等）+ `cd frontend && npm install` |
| 浏览器 | Chrome 已安装；`chrome-ws` 存在（superpowers-chrome 插件自带，**零安装**） |

> ★ **LLM 模式**：本套断言依赖**确定性输出**（SQL 含 `gmv_ex_tax`、七步全绿、canvas 数等）。
> 脚本在自行拉起后端时会注入 `DBA_LLM_USE_FAKE=true`（该开关优先级最高，即使 `.env` 开了
> `DBA_LLM_ALLOW_DEV=true` 也会走演示替身）。
> **若复用已在运行的后端**，其 LLM 模式未知 —— 跑确定性回归前请先停掉它。
> 想验证真实模型随问题变化，另行人工验收（断言应放宽为「无 `run.error`、面板 ≥2、每个
> `dataset.ref` 都存在于 `data_sources`」）。

`seed --demo` 会建演示事实表 `fact_sales`（45 天，日期相对 `CURDATE()`），
并把 `admin/admin123`、`analyst1/analyst123` 的口令写成真 `sha256`。

## 用法

```bash
# 直接跑（条件不足时打印 SKIP 并退出码 3）
bash tests/e2e/browser/run_browser_e2e.sh

# 严格模式：条件不足视为失败（CI 用）
DBA_E2E_REQUIRE=1 bash tests/e2e/browser/run_browser_e2e.sh

# 经 pytest（默认 skip，不拖慢常规 CI）
DBA_E2E_BROWSER=1 uv run pytest tests/e2e/test_browser_e2e.py -v
```

**退出码**：`0` 全部通过 / `1` 有断言失败 / `3` 环境条件不足（skip）。

脚本会：复用已在运行的后端（:8000）与前端（:5173），否则自行拉起；产物（截图 / 页面 HTML / 服务日志）
落在 `tests/e2e/browser/artifacts/<时间戳>/`（已 gitignore）。

## 断言矩阵

| 编号 | 功能点 | 断言 |
|---|---|---|
| V1 | 登录页渲染 | `.login-form` 存在、输入框为 `username`/`password` |
| V2 | 错误口令被拒 | `.hint--error` 文案含「用户名或口令错误」 |
| V3 | 登录成功 | `.query-bar` 出现、`localStorage['dba_token']` 非空 |
| V4 | 路由守卫 | 登录后离开 `/login` |
| V5 | 主页双栏 | `.panel-title` ≥ 2（对话 / 数据大屏） |
| V7 | **七步进度全绿** | `.step-list .badge--done` == **7** |
| V10 | SQL 折叠区 | 文本含 `fact_sales` 与 `gmv_ex_tax` |
| V11 | 大屏面板 | `.panel-card` ≥ 1 且标题含 `GMV`（演示驾驶舱实际 **10 个**面板） |
| V12 | 结论解读 | `.bubble` 有内容（流式渲染完成） |
| V13 | **图表真实渲染** | `.panel-card canvas` ≥ 1（演示驾驶舱实际 **5 个**：地图/环形/仪表盘/堆叠柱/趋势；4 个 KPI 与排行榜为 HTML） |
| T8 | 大屏落库 | `GET /dashboards/{id}` 返回 panels ≥ 1 且 `data_sources[].rows` ≥ 1 |

## 踩过的坑（改动本套时务必留意）

1. **受控输入不能用 `chrome-ws fill`**。`fill` 走 CDP 的 `Input.insertText`，只改 DOM 值、
   **不触发 React 的 onChange**（React 的 `_valueTracker` 把这次变更视为「无变化」而跳过），
   提交时读取的仍是旧 state —— 实测表现为「password 恒为空 → 登录失败」。
   必须用 `cw_set_input`：原型链上的 value setter 写入 + 派发冒泡的 `input` 事件。
2. **Chrome 是命令的子进程**。跨命令调用会 `ECONNREFUSED 127.0.0.1:9222`（Chrome 随命令结束被回收）。
   所以 `start` / 操作 / 断言必须在**同一进程组**内完成（本脚本天然满足）。
3. **大屏 spec 的落库时序**。后端在流水线结束后、**发 `run.finished` 之前**落库；
   前端以 `status === 'done'` 为水合门槛（不是在 `dashboard.spec.ready` 时回拉）。
   两侧顺序反了会形成竞态 → 图表恒显示「暂无数据」（已修，改代码时勿回退）。
4. **`chrome-ws eval` 返回 JSON**，字符串带引号（如 `"admin"`），断言比较前需去引号（`cw_eval` 已处理）。
5. Chrome 用独立 profile（`C:\temp\chrome-debug`），**不会污染日常浏览器**。

## 未覆盖（浏览器里验不了，勿写进断言）

| 项 | 原因 | 由谁覆盖 |
|---|---|---|
| 行级权限注入 | 浏览器链路 `_role_ids=[]`（前端不传角色），`RowScopeCompiler` 返回 `None` 故不注入 | `tests/e2e/test_e2e_chatbi_smoke.py`（stub `_role_ids=[1]`） |
| SQL 自愈重试 | `DemoLLMClient` 是确定性且 SQL 正确，**无法复现**校验失败 | 同上（stub router 可注入坏 SQL） |
| 预算告警 / 降级 banner | 需真实用量触发，demo 不产生 | `tests/qa/` 相关用例 |
| 成本展示 | `run.finished` 不带 `usage`（`_run_bg` 未下发） | 后续补齐时再加断言 |
| SSE 断线重连（`Last-Event-ID`） | demo 下 Run 秒级完成，事件在首连回放即终态 | 需人为断网，列为 P2 |
| 观测 / FinOps 页面 | **前端未开发**，无 UI 入口 | — |
