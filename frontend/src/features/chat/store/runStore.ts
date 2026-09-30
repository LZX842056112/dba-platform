// ★ 流式事件 reducer（对齐《设计方案 v2》§9.2）。
//
// v2 的四处关键修正（v1 的 bug，照抄会错）：
//   ① 必须有 `run.started` 分支：它**重置整个 store**。v1 缺此分支、也没有 reset()，
//      同一会话第二次提问会残留上一次的 panels / narration / error ——
//      用户看到的是「新问题 + 旧大屏」拼在一起。
//   ② 面板按 `panel_id` **upsert**（v1 用数组 append，重算/重放时同一面板会出现两份）。
//   ③ `lastSeq` 幂等：断线重连后服务端会重放（Last-Event-ID 之后），
//      用 seq 单调递增去重，避免「面板重复渲染两份」。
//   ④ 面板只带 `dataset.ref`，真正的行数据在 `data_sources` 里（§9.5）；
//      数据源不随 SSE 增量下发，需在 `dashboard.spec.ready` 后按 dashboard_id 回拉整份大屏 JSON。

import { create } from 'zustand';
import type { DashboardSpec, DataSource, Layout, PanelSpec } from '../../../types/dashboard';
import type { EventEnvelope, UsageData } from '../../../types/events';

export type RunStatus = 'idle' | 'running' | 'done' | 'error';

export interface StepState {
  status: 'running' | 'done' | 'retrying';
  attempt: number;
  durationMs?: number;
}

export interface RetryingInfo {
  attempt: number;
  code: string;
  message: string;
}

export interface RunError {
  code: string;
  message: string;
  retryable: boolean;
  hint?: string;
}

export interface RunState {
  traceId: string | null;
  status: RunStatus;
  lastSeq: number;
  steps: Record<string, StepState>;
  sql: string | null;
  retrying: RetryingInfo | null;
  panels: Record<string, PanelSpec>;
  panelOrder: string[];
  layout: Layout | null;
  /** 大屏 id（`dashboard.spec.ready` 提供），用于回拉完整 JSON（含 data_sources）。 */
  dashboardId: string | null;
  dataSources: Record<string, DataSource>;
  narration: string;
  usage: UsageData | null;
  error: RunError | null;
  /** 顶部提示条（预算告警 / 降级等）。 */
  banner: { kind: 'warn' | 'info' | 'error'; text: string } | null;
}

const initialRunState: RunState = {
  traceId: null,
  status: 'idle',
  lastSeq: 0,
  steps: {},
  sql: null,
  retrying: null,
  panels: {},
  panelOrder: [],
  layout: null,
  dashboardId: null,
  dataSources: {},
  narration: '',
  usage: null,
  error: null,
  banner: null,
};

/** 合并数据源增量（按 ref upsert）。 */
function mergeDataSources(
  prev: Record<string, DataSource>,
  incoming: DataSource[] | undefined,
): Record<string, DataSource> {
  if (!incoming || incoming.length === 0) return prev;
  const next = { ...prev };
  for (const ds of incoming) next[ds.ref] = ds;
  return next;
}

export interface RunStore extends RunState {
  apply: (event: EventEnvelope) => void;
  /** 回拉整份大屏 JSON（§9.5）后合并 data_sources / layout / panels。 */
  hydrateDashboard: (spec: Partial<DashboardSpec> | null | undefined) => void;
  reset: () => void;
}

export const useRunStore = create<RunStore>((set, get) => ({
  ...initialRunState,

  reset: () => set(() => ({ ...initialRunState })),

  hydrateDashboard: (spec) => {
    if (!spec || typeof spec !== 'object') return;
    set((s) => {
      const panels = { ...s.panels };
      for (const panel of spec.panels ?? []) panels[panel.panel_id] = panel;
      return {
        panels,
        panelOrder: Array.from(new Set([...s.panelOrder, ...Object.keys(panels)])),
        layout: spec.layout ?? s.layout,
        dashboardId: spec.dashboard_id ?? s.dashboardId,
        dataSources: mergeDataSources(s.dataSources, spec.data_sources),
      };
    });
  },

  apply: (event) => {
    // ★ 幂等：断线重连后可能收到重复事件
    if (event.seq <= get().lastSeq) return;

    // seq 作为通用「已处理水位」在此统一推进；各分支只负责自身状态的增量。
    const bump = { lastSeq: event.seq };

    switch (event.event) {
      case 'run.started': {
        // ★ 重置整个 store（v1 缺失此分支 → 残留旧大屏）
        set(() => ({
          ...initialRunState,
          traceId: event.trace_id,
          status: 'running',
          lastSeq: event.seq,
        }));
        return;
      }
      case 'agent.step.started': {
        const data = event.data as { step: string; attempt: number };
        set((s) => ({
          ...bump,
          steps: { ...s.steps, [data.step]: { status: 'running', attempt: data.attempt } },
        }));
        return;
      }
      case 'agent.step.retrying': {
        const data = event.data as { step: string; attempt: number };
        set((s) => ({
          ...bump,
          steps: {
            ...s.steps,
            [data.step]: {
              ...(s.steps[data.step] ?? { attempt: data.attempt }),
              status: 'retrying',
              attempt: data.attempt,
            },
          },
        }));
        return;
      }
      case 'agent.step.finished': {
        const data = event.data as {
          step: string;
          duration_ms: number;
          attempt?: number;
          status?: string;
        };
        const prev = get().steps[data.step];
        set((s) => ({
          ...bump,
          steps: {
            ...s.steps,
            [data.step]: {
              status: 'done',
              attempt: prev?.attempt ?? data.attempt ?? 1,
              durationMs: data.duration_ms,
            },
          },
        }));
        return;
      }
      case 'sql.generated': {
        const data = event.data as { sql: string };
        set(() => ({ ...bump, sql: data.sql }));
        return;
      }
      case 'sql.validation.failed': {
        const data = event.data as { attempt: number; code: string; message: string };
        set(() => ({
          ...bump,
          retrying: { attempt: data.attempt, code: data.code, message: data.message },
        }));
        return;
      }
      case 'sql.retry.resolved': {
        const data = event.data as { sql: string };
        set(() => ({ ...bump, retrying: null, sql: data.sql }));
        return;
      }
      case 'sql.executing': {
        const data = event.data as { sql?: string };
        set((s) => ({ ...bump, sql: data.sql ?? s.sql }));
        return;
      }
      case 'sql.executed': {
        set(() => ({ ...bump }));
        return;
      }
      case 'dashboard.spec.delta': {
        // ★ 按 panel_id upsert（v1 数组 append → 重算/重放出重复）
        const data = event.data as { panels: PanelSpec[]; data_sources?: DataSource[] };
        set((s) => {
          const next = { ...s.panels };
          for (const panel of data.panels ?? []) next[panel.panel_id] = panel;
          return {
            ...bump,
            panels: next,
            panelOrder: Array.from(new Set([...s.panelOrder, ...Object.keys(next)])),
            dataSources: mergeDataSources(s.dataSources, data.data_sources),
          };
        });
        return;
      }
      case 'dashboard.spec.ready': {
        const data = event.data as {
          dashboard_id?: string;
          layout?: Layout;
          data_sources?: DataSource[];
        };
        set((s) => ({
          ...bump,
          dashboardId: data.dashboard_id ?? s.dashboardId,
          layout: data.layout ?? s.layout,
          dataSources: mergeDataSources(s.dataSources, data.data_sources),
        }));
        return;
      }
      case 'narration.delta': {
        const data = event.data as { text_delta: string };
        set((s) => ({ ...bump, narration: s.narration + (data.text_delta ?? '') }));
        return;
      }
      case 'budget.warning': {
        const data = event.data as { used_pct: number };
        set(() => ({
          ...bump,
          banner: { kind: 'warn', text: `预算已用 ${Math.round((data.used_pct ?? 0) * 100)}%` },
        }));
        return;
      }
      case 'budget.downgraded': {
        const data = event.data as { from_model: string; to_model: string; reason: string };
        set(() => ({
          ...bump,
          banner: {
            kind: 'info',
            text: `为控制成本已切换至更快模型：${data.from_model} → ${data.to_model}`,
          },
        }));
        return;
      }
      case 'budget.blocked': {
        const data = event.data as { reason: string };
        set(() => ({ ...bump, banner: { kind: 'error', text: `预算拦截：${data.reason}` } }));
        return;
      }
      case 'run.finished': {
        const data = event.data as { usage?: UsageData };
        set(() => ({ ...bump, status: 'done', usage: data.usage ?? null }));
        return;
      }
      case 'run.error': {
        const data = event.data as {
          code: string;
          message: string;
          retryable: boolean;
          hint?: string;
        };
        set(() => ({
          ...bump,
          status: 'error',
          error: {
            code: data.code,
            message: data.message,
            retryable: data.retryable,
            hint: data.hint,
          },
        }));
        return;
      }
      case 'run.aborted': {
        const data = event.data as { reason: string };
        set(() => ({
          ...bump,
          status: 'error',
          error: { code: 'RUN_ABORTED', message: data.reason ?? '运行被中断', retryable: false },
        }));
        return;
      }
      default: {
        // 其余事件（含 heartbeat / agent.step.delta）只推进水位
        set(() => ({ ...bump }));
        return;
      }
    }
  },
}));

/** 已到达面板的有序列表（供渲染器消费）。 */
export function orderedPanels(state: RunState): PanelSpec[] {
  return state.panelOrder.map((id) => state.panels[id]).filter((p): p is PanelSpec => Boolean(p));
}
