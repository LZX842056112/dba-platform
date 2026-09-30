// 应用根：Provider 装配 + 外壳布局（§9.1 app/App.tsx）。

import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from './providers/AuthProvider';
import { QueryProvider } from './providers/QueryProvider';
import { AppRoutes } from './routes';

export function App() {
  return (
    <QueryProvider>
      <AuthProvider>
        <BrowserRouter>
          <div className="app-shell">
            <header className="app-header">
              <strong>数据大屏 · 多 Agent 平台</strong>
              <span className="dim">ChatBI 最小集（对话 → 七步流水线 → 出图）</span>
            </header>
            <main className="app-main">
              <AppRoutes />
            </main>
          </div>
        </BrowserRouter>
      </AuthProvider>
    </QueryProvider>
  );
}
