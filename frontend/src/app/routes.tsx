// 路由表（最小集：登录页 + 对话页；其余页面按 P2 扩展）。

import { Navigate, Route, Routes } from 'react-router-dom';
import { ChatSessionPage } from '../pages/chatbi/ChatSessionPage';
import { LoginPage } from '../pages/login/LoginPage';
import { RequireAuth } from './providers/AuthProvider';

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <ChatSessionPage />
          </RequireAuth>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
