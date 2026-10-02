// FinOps 接口（对齐 backend api/v1/finops.py，§7.5）。

import { api } from '../../api/client';

export interface CostSummary {
  total: number;
  by_module: Record<string, number>;
  by_biz_line: Record<string, number>;
  by_model: Record<string, number>;
}

export interface CostTimeseries {
  series: Array<{ ts: string; cost_micro_usd: number; group_by: string; group: string }>;
}

export interface Coverage {
  priced_ratio: number;
  unpriced_calls: number;
  by_provider: Array<Record<string, unknown>>;
  total_calls?: number;
}

export interface CacheStats {
  hit_rate: number;
  saved_micro_usd: number;
  by_task: Array<Record<string, unknown>>;
}

export interface ReuseCurve {
  biz_line_id: number;
  days: number;
  reuse_series: Array<Record<string, unknown>>;
  token_series: Array<Record<string, unknown>>;
  correlation: number | null;
  total_saved_micro_usd: number;
  note?: string;
}

export interface GuardrailPolicy {
  enabled: boolean;
  allow_downgrade: boolean;
  allow_compress: boolean;
  allow_rate_limit: boolean;
  allow_circuit_break: boolean;
  exemption_priority: number;
  breaker_window_s: number;
  breaker_consecutive_windows: number;
  breaker_cooldown_s: number;
  max_downgrades_per_run: number;
  kill_switch: boolean;
}

export interface FinopsRecommendation {
  reco_id: string;
  scope_type?: string;
  scope_id?: string;
  status?: string;
  type?: string;
  description?: string;
  reason?: string;
  estimated_saving_micro_usd?: number;
}

export function getCostSummary(): Promise<CostSummary> {
  return api.get<CostSummary>('/finops/cost/summary');
}

export function getCostTimeseries(): Promise<CostTimeseries> {
  return api.get<CostTimeseries>('/finops/cost/timeseries');
}

export function getTopSpenders(): Promise<Array<{ key: string; cost_micro_usd: number }>> {
  return api.get<Array<{ key: string; cost_micro_usd: number }>>('/finops/cost/top-spenders');
}

export function getCoverage(): Promise<Coverage> {
  return api.get<Coverage>('/finops/cost/coverage');
}

export function getCacheStats(): Promise<CacheStats> {
  return api.get<CacheStats>('/finops/cache/stats');
}

export function getReuseCurve(): Promise<ReuseCurve> {
  return api.get<ReuseCurve>('/finops/curve/reuse-vs-token');
}

export function getBudgets(): Promise<Array<Record<string, unknown>>> {
  return api.get<Array<Record<string, unknown>>>('/finops/budgets');
}

export function getBudgetUsage(id: number): Promise<Record<string, unknown>> {
  return api.get<Record<string, unknown>>(`/finops/budgets/${id}/usage`);
}

export function getRecommendations(): Promise<FinopsRecommendation[]> {
  return api.get<FinopsRecommendation[]>('/finops/recommendations');
}

export function getLoopAlerts(): Promise<Array<Record<string, unknown>>> {
  return api.get<Array<Record<string, unknown>>>('/finops/loop-alerts');
}

export function getGuardrailPolicy(): Promise<GuardrailPolicy> {
  return api.get<GuardrailPolicy>('/finops/guardrail/policy');
}

export function updateGuardrailPolicy(patch: Partial<GuardrailPolicy>): Promise<GuardrailPolicy> {
  return api.patch<GuardrailPolicy>('/finops/guardrail/policy', patch);
}

export function applyRecommendation(id: string, dryRun: boolean): Promise<Record<string, unknown>> {
  return api.post<Record<string, unknown>>(
    `/finops/recommendations/${encodeURIComponent(id)}/apply`,
    { dry_run: dryRun },
  );
}

export function rejectRecommendation(id: string): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(
    `/finops/recommendations/${encodeURIComponent(id)}/reject`,
    { reason: '演示拒绝' },
  );
}
