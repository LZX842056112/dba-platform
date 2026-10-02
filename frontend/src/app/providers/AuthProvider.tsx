// 鉴权 Provider（§9.1）：从 localStorage 读回 JWT 并注入 API 客户端。
// SSE 不走这里——EventSource 带不上 Authorization，改用一次性 stream_ticket（P0-7）。

import { useEffect, type ReactNode } from 'react';
import { Navigate, Outlet } from 'react-router-dom';
import { setAuthToken } from '../../api/client';

const TOKEN_KEY = 'dba_token';

interface Props {
  children?: ReactNode;
}

export function AuthProvider({ children }: Props) {
  useEffect(() => {
    const token = window.localStorage.getItem(TOKEN_KEY);
    if (token) setAuthToken(token);
  }, []);

  return <>{children}</>;
}

/** 路由守卫：无 token 跳登录页（`/login`）。 */
export function RequireAuth({ children }: Props) {
  const token = window.localStorage.getItem(TOKEN_KEY);
  if (!token) return <Navigate to="/login" replace />;
  return <>{children ?? <Outlet />}</>;
}

export function login(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
  setAuthToken(token);
}

export function logout(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  setAuthToken(null);
}
