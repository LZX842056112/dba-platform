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
4. `dashboard.spec.ready` 后按 `dashboard_id` 回拉整份大屏 JSON 补全 `data_sources`（面板只带 `dataset.ref`）。

## 未实现 / 未验证（如实标注）

- **未实现（P2）**：观测页、FinOps 页、语义管理页、技能页；`mode='s3'` 数据源的前端预签名直拉（§9.5 硬约定 2）。
- **未验证**：真实浏览器下的 SSE 长连接、断线重连、ECharts 实际渲染效果（CI 仅能保证类型检查与打包通过）。
- `run.finished` 的 `usage` 字段：后端当前下发 `{status, steps_run, retries}`，成本展示为可选；不一致处已在 `types/events.ts` 标注。
