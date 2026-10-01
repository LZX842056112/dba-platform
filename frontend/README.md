# 前端最小集 · 数据大屏 × 多 Agent 平台

对齐《数据大屏-多Agent平台-三模块开发设计方案-v2》**§9（前端分层与交互协议）**。
本目录只交付**能跑通主链路的最小集**，不含 20 个完整页面（观测 / FinOps 等属 P2）。

## 技术栈（§9.1）

| 依赖 | 用途 |
|---|---|
| Vite + React 18 + TypeScript | 构建与框架 |
| ECharts 5（`echarts/core` 按需注册） | 图表渲染（§9.6） |
| TanStack Query | 服务端状态（大屏 JSON 回拉） |
| zustand | 客户端状态（`runStore` 幂等 reducer，§9.2） |
| react-router-dom | 路由（最小集仅 `/` 对话页） |

> 包管理器：设计文档写 `pnpm`；本机无 pnpm 时用 **npm**（脚本等价，已如实报告）。

## 目录结构（§9.1）

```
src/
├── api/            client.ts（统一 fetch）/ sse.ts（★ Last-Event-ID 续传）
├── app/            App.tsx / routes.tsx / providers/
├── components/charts/  EchartsBase.tsx（★ §9.6 基座）
├── features/
│   ├── chat/       api/ components/ hooks/ store/runStore.ts（★ 幂等 reducer）
│   └── dashboard/  components/（DashboardGrid / PanelRenderer …）optionBuilder.ts selectors.ts
├── lib/            echarts.ts / format.ts / schema.ts
├── pages/chatbi/   ChatSessionPage.tsx
├── styles/         tokens.css / global.css
└── types/          dashboard.ts / events.ts / api.d.ts
```

## 命令

```bash
npm install          # 安装依赖
npm run dev          # 本地开发（/api 代理到 127.0.0.1:8000，见 vite.config.ts）
npm run build        # tsc --noEmit && vite build
npm run lint         # tsc --noEmit
npm run gen:api      # 由后端 OpenAPI 生成 src/types/api.d.ts（U27）
```

### 本机安装注意（Windows）

1. **包管理器**：本机无 `pnpm`（`pnpm: command not found`），改用 **npm**，脚本等价。
2. **`npm install` 首次可能因 esbuild 的 postinstall 报 `EBUSY`**（Windows 上 spawn node 被占用）：
   直接**重跑一次** `npm install` 即可（缓存已热，秒级完成）。
3. **npm 可选依赖 bug（npm/cli#4828）**：`@rollup/rollup-win32-x64-msvc` / `@esbuild/win32-x64`
   可能不被安装，导致 `vite build` 报 `Cannot find module @rollup/rollup-win32-x64-msvc`。
   本仓库已在 `package.json` 的 `optionalDependencies` 显式声明这两个平台二进制作为兜底
   （非本平台会被 npm 按 `os`/`cpu` 自动跳过，其他平台不受影响）。
4. **`gen:api` 需要 `/api` 的 OpenAPI 文件**：先在仓库根执行导出（已生成 `apps/platform/openapi.json`）：

   ```bash
   uv run python -c "import json; from dba.main import create_app; json.dump(create_app().openapi(), open('apps/platform/openapi.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)"
   ```


## 主链路（对齐 §9.3 事件表）

1. `POST /chat/sessions`（懒建会话）→ `POST /chat/sessions/{sid}/query` 返回 `{trace_id, stream_ticket}`（**P0-7**：不直接返回 SSE）。
2. `EventSource` 打开 `GET /stream/runs/{trace_id}?ticket=…`。
   - ⚠️ 后端以**命名事件**（`event: run.started`）推送，故客户端对每个事件名 `addEventListener`，**不能用 `onmessage`**。
   - 信封的 `seq` 取自 SSE 的 `id:` 字段；浏览器据此自动带 `Last-Event-ID` 续传；`runStore` 用 `seq` 幂等去重。
3. `runStore.apply(event)` 分派：`run.started` 重置、`agent.step.*` 进度、`sql.*`/自愈提示、`dashboard.spec.delta` 按 `panel_id` upsert、`dashboard.spec.ready` 锁定布局。
4. 收到 `run.finished`（`status === 'done'`）后按 `dashboard_id` 回拉整份大屏 JSON 补全 `data_sources`
   （面板只带 `dataset.ref`）。★ 门槛用终态而非 `dashboard.spec.ready`：后者在流水线中途就到达，
   此时后端尚未落库 → 会拿到空 spec → 图表恒「暂无数据」。

## 驾驶舱能力（面板类型与图表）

| `panel.kind` | 渲染 | 关键 `style` |
|---|---|---|
| `chart` | `EchartsBase`（深色主题 `dba-dark`） | 见下表 |
| `metric_card` | 指标卡；`variant="flip"` 触发**数字翻牌动画** | `unit` `trend` `trendValue` |
| `ranking` | 排行榜（名次 + 名称 + 进度条 + 数值） | `sort` `topN` |
| `table` / `text` | 明细表 / 文本 | — |

`chart.type` ∈ `line | bar | pie | scatter | map | gauge`，配置一律走 `panel.style`：

| type | 形态与要点 |
|---|---|
| `map` | 中国地图 choropleth（`registerMap` + `geoIndex`）；`style.mapScatter` 叠加涟漪散点（需 `encoding.lon/lat`） |
| `gauge` | 仪表盘；`style.gaugeMax` / `gaugeTarget`（目标线） |
| `pie` | `style.pieVariant ∈ {pie, donut, rose, ring}` |
| `bar` | `style.orientation="horizontal"` 条形排行；`stack` 堆叠；`showBackground` 进度条底 |
| `line` | `style.area` 面积图；`stack` 堆叠面积 |
| `scatter` | `encoding.size` 控制气泡大小 |

**深色主题**：`lib/echartsTheme.ts` 注册 `dba-dark`，`EchartsBase` 默认启用（此前用 ECharts 默认浅色主题，深底上文字几乎不可读）。

**地图合规**：GeoJSON 随仓库内置（`src/assets/geo/`，含 34 省级 + 九段线），**懒加载**（582KB 不进主包）；来源与领土完整性校验见该目录 `README.md`。

**健壮性**：`PanelErrorBoundary` 保证单个面板渲染异常只降级该面板，**不会整页白屏**。

## 未实现 / 未验证（如实标注）

- **未实现（P2）**：观测页、FinOps 页、语义管理页、技能页；`mode='s3'` 数据源的前端预签名直拉（§9.5 硬约定 2）。
- **已验证（真实浏览器）**：登录（含错误口令）、提问 → 七步进度 → SQL 折叠 → ECharts 真实渲染，
  由 `tests/e2e/browser/run_browser_e2e.sh` 用真 Chrome 断言（见该目录 README）。
  **仍未验证**：SSE 断线重连（`Last-Event-ID`，需人为断网）、长连接下的内存行为。
- `run.finished` 的 `usage` 字段：后端当前下发 `{status, steps_run, retries}`，成本展示为可选；不一致处已在 `types/events.ts` 标注。
