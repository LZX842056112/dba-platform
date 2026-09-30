// 路由表（最小集：仅对话页；其余页面按 P2 扩展）。

import { Navigate, Route, Routes } from 'react-router-dom';
import { ChatSessionPage } from '../pages/chatbi/ChatSessionPage';

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<ChatSessionPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
