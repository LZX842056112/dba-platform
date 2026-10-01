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

let authToken: string | null = null;

/** 设置 / 清除 Bearer JWT（内存态；持久化由 AuthProvider 负责）。 */
export function setAuthToken(token: string | null): void {
  authToken = token;
}

function buildHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set('Content-Type', 'application/json');
  if (authToken) headers.set('Authorization', `Bearer ${authToken}`);
  return headers;
}

/** 统一 fetch：JSON 解析 + 错误体映射 + trace_id 透传。 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  traceId?: string,
): Promise<T> {
  const headers = buildHeaders(init.headers);
  if (traceId) headers.set('X-Trace-Id', traceId);

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
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
