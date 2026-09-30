// SSE 事件协议（对齐《设计方案 v2》§9.3）。
// 统一信封 + 各事件的 payload 类型。

import type { DataSource, Layout, PanelSpec } from './dashboard';

export type EventType =
  | 'run.started'
  | 'agent.step.started'
  | 'agent.step.delta'
  | 'agent.step.finished'
  | 'agent.step.retrying'
  | 'sql.generated'
  | 'sql.validation.failed'
  | 'sql.retry.resolved'
  | 'sql.executing'
  | 'sql.executed'
  | 'dashboard.spec.delta'
  | 'dashboard.spec.ready'
  | 'narration.delta'
  | 'budget.warning'
  | 'budget.downgraded'
  | 'budget.blocked'
  | 'run.finished'
  | 'run.error'
  | 'run.aborted'
  | 'heartbeat';

export interface EventEnvelope<T = Record<string, unknown>> {
  event: EventType;
  seq: number;
  trace_id: string;
  ts: number;
  data: T;
}

export interface RunStartedData {
  module?: string;
  user_id?: number | null;
  biz_line_id?: number | null;
}

export interface StepStartedData {
  step: string;
  attempt: number;
}

export interface StepFinishedData {
  step: string;
  duration_ms: number;
  summary?: string;
  confidence?: number;
}

export interface SqlValidationFailedData {
  attempt: number;
  code: string;
  message: string;
}

export interface SqlRetryResolvedData {
  attempt: number;
  sql: string;
}

export interface SqlExecutingData {
  sql_hash?: string;
  sql?: string;
  timeout_s?: number;
  max_rows?: number;
}

export interface SqlExecutedData {
  rows_returned: number;
  exec_ms: number;
  scope_injected: boolean;
}

export interface DashboardDeltaData {
  panels: PanelSpec[];
  layoutPatch?: unknown;
  /** 兼容：数据源若随增量下发则一并携带（生产实现由 ready 后回拉整份大屏）。 */
  data_sources?: DataSource[];
}

export interface DashboardReadyData {
  dashboard_id: string;
  version: number;
  layout: Layout;
  cols?: number;
  rowHeight?: number;
  data_sources?: DataSource[];
}

export interface NarrationDeltaData {
  text_delta: string;
}

export interface UsageData {
  prompt_tokens: number;
  completion_tokens: number;
  cost_micro_usd: number;
  cache_hit: boolean;
}

export interface BudgetWarningData {
  scope: string;
  used_pct: number;
  action: string;
}

export interface BudgetDowngradedData {
  from_model: string;
  to_model: string;
  reason: string;
  scope: string;
}

export interface RunFinishedData {
  status: string;
  /** 后端 chatbi.py 实际下发：`{status, steps_run, retries}`；usage 为可选（§9.3 信封一致化前）。 */
  steps_run?: string[];
  retries?: number;
  latency_ms?: number;
  usage?: UsageData;
}

export interface RunErrorData {
  code: string;
  message: string;
  retryable: boolean;
  hint?: string;
}

/** 终态事件（收到即关闭连接）。 */
export const TERMINAL_EVENTS: EventType[] = ['run.finished', 'run.error', 'run.aborted'];

export function isTerminal(event: EventType): boolean {
  return TERMINAL_EVENTS.includes(event);
}
