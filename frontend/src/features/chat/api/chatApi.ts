// ChatBI 对话相关接口（对齐后端 api/v1/chatbi.py 与 §7.3 / §9.3）。
//
// ★ P0-7 关键点：`POST /chat/sessions/{sid}/query` **不直接返回 SSE 流**，
//   而是「立即返回 trace_id + 一次性 stream_ticket」；前端再用 `?ticket=` 打开
//   `GET /stream/runs/{trace_id}`。原因：EventSource 带不上 Bearer JWT。

import { api } from '../../../api/client';
import type { DashboardSpec } from '../../../types/dashboard';

export interface StartQueryResult {
  trace_id: string;
  stream_ticket: string;
  expires_in: number;
}

export interface SessionResult {
  session_id: string;
}

/** 新建会话。 */
export async function createSession(bizLineId?: number | null): Promise<SessionResult> {
  return api.post<SessionResult>('/chat/sessions', {
    biz_line_id: bizLineId ?? null,
    title: '新会话',
  });
}

/** 发起一次提问：返回 trace_id + 一次性 ticket。 */
export async function startQuery(sessionId: string, question: string): Promise<StartQueryResult> {
  return api.post<StartQueryResult>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/query`,
    { question },
  );
}

/** 断线重连时重新换取 ticket。 */
export async function renewStreamTicket(traceId: string): Promise<{ stream_ticket: string }> {
  return api.post<{ stream_ticket: string }>('/chat/stream-ticket', { trace_id: traceId });
}

/** 回拉整份大屏 JSON（含 data_sources）——面板只带 dataset.ref，行数据在此。 */
export async function getDashboardSpec(dashboardId: string): Promise<DashboardSpec | null> {
  const spec = await api.get<Partial<DashboardSpec>>(`/dashboards/${encodeURIComponent(dashboardId)}`);
  if (!spec || Object.keys(spec).length === 0) return null;
  return spec as DashboardSpec;
}
