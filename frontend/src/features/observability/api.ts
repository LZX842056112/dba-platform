// 观测（Observability）接口（对齐 backend api/v1/observability.py，§7.4）。

import { api } from '../../api/client';

export interface OverviewKpi {
  running_now: number;
  cost_today_micro: number;
  success_rate: number;
  silent_failures: number;
}

export interface OverviewResponse {
  kpi_cards: OverviewKpi;
  trend: { granularity: string; series: Array<Record<string, unknown>> };
  top_agents: Array<Record<string, unknown>>;
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

export function getOverview(): Promise<OverviewResponse> {
  return api.get<OverviewResponse>('/obs/overview');
}

export function getTopology(): Promise<TopologyResponse> {
  return api.get<TopologyResponse>('/obs/topology');
}

export function getRuns(): Promise<Array<Record<string, unknown>>> {
  return api.get<Array<Record<string, unknown>>>('/obs/runs');
}

export function getRunDetail(traceId: string): Promise<Record<string, unknown>> {
  return api.get<Record<string, unknown>>(`/obs/runs/${encodeURIComponent(traceId)}`);
}

export function getSelfCost(): Promise<SelfCost> {
  return api.get<SelfCost>('/obs/self-cost');
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

export function getAnomalies(): Promise<Array<Record<string, unknown>>> {
  return api.get<Array<Record<string, unknown>>>('/obs/anomalies');
}

export function ackAnomaly(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/obs/anomalies/${encodeURIComponent(id)}/ack`, {});
}

export function resolveAnomaly(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/obs/anomalies/${encodeURIComponent(id)}/resolve`, {});
}
