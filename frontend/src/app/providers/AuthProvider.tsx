// 有会话时打开受保护页面。

import type { ReactNode } from 'react';
import { Navigate, Outlet } from 'react-router-dom';
import { getAuthToken } from '../../api/client';

interface Props {
  children?: ReactNode;
}

export function RequireAuth({ children }: Props) {
  if (!getAuthToken()) return <Navigate to="/login" replace />;
  return <>{children ?? <Outlet />}</>;
}
