// API 客户端：统一 base、错误码映射、trace_id 透传（§9.1 api/client.ts）。

export interface ApiErrorBody {
  code?: string;
  message?: string;
  detail?: unknown;
  trace_id?: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly traceId: string | null;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message ?? `请求失败（HTTP ${status}）`);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code ?? `HTTP_${status}`;
    this.traceId = body.trace_id ?? null;
  }
}

const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1';
const TOKEN_KEY = 'dba_token';

export function getAuthToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setAuthToken(token: string | null): void {
  if (token === null) window.localStorage.removeItem(TOKEN_KEY);
  else window.localStorage.setItem(TOKEN_KEY, token);
}

function buildHeaders(token: string | null, extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return headers;
}

/** 统一 fetch：JSON 解析 + 错误体映射 + trace_id 透传。 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  traceId?: string,
): Promise<T> {
  const token = getAuthToken();
  const headers = buildHeaders(token, init.headers);
  if (traceId) headers.set('X-Trace-Id', traceId);

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (
    response.status === 401 && path !== '/auth/login' &&
    token === getAuthToken() && window.location.pathname !== '/login'
  ) {
    setAuthToken(null);
    window.location.replace('/login?expired=1');
  }
  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : {};

  if (!response.ok) {
    throw new ApiError(response.status, payload as ApiErrorBody);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, traceId?: string): Promise<T> => apiFetch<T>(path, {}, traceId),
  post: <T>(path: string, body: unknown, traceId?: string): Promise<T> =>
    apiFetch<T>(path, { method: 'POST', body: JSON.stringify(body) }, traceId),
  patch: <T>(path: string, body: unknown, traceId?: string): Promise<T> =>
    apiFetch<T>(path, { method: 'PATCH', body: JSON.stringify(body) }, traceId),
};

export function recentWindowQuery(hours: number): string {
  const until = new Date();
  const since = new Date(until.getTime() - hours * 3_600_000);
  return `?${new URLSearchParams({ from: since.toISOString(), to: until.toISOString() })}`;
}
