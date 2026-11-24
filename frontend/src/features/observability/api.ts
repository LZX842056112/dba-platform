// 观测（Observability）接口（对齐 backend api/v1/observability.py，§7.4）。
//
// ★ 行级返回一律给出显式接口（RunRow / RunDoc / AlertEvent）：
//   曾经统一返回 `Record<string, unknown>`，导致每个页面各自写一遍字段归一化，
//   同一份 /obs/runs 在两处用两套 typeof 判断。字段可缺省（后端降级/旧数据）时
//   用可选类型表达，页面只需处理 `null | undefined`，不必再做类型断言。

import { api, recentWindowQuery } from '../../api/client';

export interface OverviewMetric {
  value: number;
  label?: string;
}

export interface OverviewKpi {
  total_runs: OverviewMetric;
  running_now: OverviewMetric;
  cost_today_micro: OverviewMetric;
  success_rate: OverviewMetric;
  silent_failures: OverviewMetric;
}

/** 日成本时序点（metric_daily 按天可能多行，agent/model 维度）。 */
export interface TrendPoint {
  ts: string;
  value: number;
  agent_uid?: string | null;
  model?: string | null;
  biz_line_id?: number | null;
}

export interface TopAgent {
  agent_uid: string;
  runs: number;
  success_rate: number;
}

export interface OverviewResponse {
  kpi_cards: OverviewKpi;
  trend: { granularity: string; series: TrendPoint[] };
  top_agents: TopAgent[];
}

/** `/obs/runs` 的行（storage/mysql/models.py Run）。 */
export interface RunRow {
  trace_id: string;
  started_at?: string | null;
  module?: string;
  status?: string;
  latency_ms?: number | null;
  agent_uid?: string | null;
  cost_micro_usd?: number | null;
  error_code?: string | null;
  tokens_in?: number;
  tool_calls?: number;
}

/** run_doc 的 span（agent-runtime telemetry 埋点）。 */
export interface RunSpan {
  span_id: string;
  parent_span_id?: string | null;
  name?: string;
  kind?: string;
  /** 绝对墙钟（毫秒时间戳）。 */
  start_ms?: number | null;
  duration_ms?: number | null;
  status?: string;
  error_type?: string | null;
  error_message?: string | null;
}

/** `/obs/runs/{trace_id}`：run_doc 顶层字段不保证存在，只有 trace_id + spans 也可。 */
export type RunDoc = Partial<RunRow> & { spans?: RunSpan[] };

/** `/obs/anomalies` 的行（MySQL alert_event）。 */
export interface AlertEvent {
  id: number;
  detected_at?: string | null;
  severity?: string;
  category?: string;
  scope_type?: string;
  scope_id?: string | null;
  metric?: string;
  observed_value?: number | string | null;
  baseline_value?: number | string | null;
  robust_zscore?: number | string | null;
  status?: string;
  trace_id?: string | null;
}

export interface TopologyResponse {
  nodes: Array<{ node_id: string; name: string; kind: string; span_count: number; error_count: number }>;
  edges: Array<{ source: string; target: string; calls: number }>;
}

export interface SkillsMetrics {
  total: number;
  used: number;
  reuse_rate: number;
  dead_count: number;
  series: Array<Record<string, unknown>>;
}

export interface MemoryMetrics {
  hit_rate: number;
  lookups: number;
  hits: number;
  series: Array<Record<string, unknown>>;
}

export interface SelfCost {
  by_module: Record<string, { cost_micro_usd: number; runs: number }>;
  total_cost_micro_usd: number;
  total_runs: number;
}

export function getOverview(hours: number): Promise<OverviewResponse> {
  return api.get<OverviewResponse>(`/obs/overview${recentWindowQuery(hours)}`);
}

export function getTopology(): Promise<TopologyResponse> {
  return api.get<TopologyResponse>('/obs/topology');
}

export function getRuns(): Promise<RunRow[]> {
  return api.get<RunRow[]>('/obs/runs');
}

export function getRunDetail(traceId: string): Promise<RunDoc> {
  return api.get<RunDoc>(`/obs/runs/${encodeURIComponent(traceId)}`);
}

export function getSelfCost(hours: number): Promise<SelfCost> {
  return api.get<SelfCost>(`/obs/self-cost${recentWindowQuery(hours)}`);
}

export function getSkillsMetrics(): Promise<SkillsMetrics> {
  return api.get<SkillsMetrics>('/obs/metrics/skills');
}

export function getMemoryMetrics(): Promise<MemoryMetrics> {
  return api.get<MemoryMetrics>('/obs/metrics/memory');
}

export function getTimeseries(): Promise<{ series: Array<Record<string, unknown>> }> {
  return api.get<{ series: Array<Record<string, unknown>> }>('/obs/metrics/timeseries');
}

export function getAnomalies(): Promise<AlertEvent[]> {
  return api.get<AlertEvent[]>('/obs/anomalies');
}

export function ackAnomaly(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/obs/anomalies/${encodeURIComponent(id)}/ack`, {});
}

export function resolveAnomaly(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/obs/anomalies/${encodeURIComponent(id)}/resolve`, {});
}
