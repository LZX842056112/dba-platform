// 认证 API（§7.2）：登录换取 JWT。
// SSE 不走 Authorization（EventSource 带不上头），改用一次性 stream_ticket（P0-7）。

import { api } from './client';

export interface LoginResult {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

/** `POST /auth/login`。失败抛 `ApiError`（提示语通常为「用户名或口令错误」）。 */
export function loginRequest(username: string, password: string): Promise<LoginResult> {
  return api.post<LoginResult>('/auth/login', { username, password });
}
