// 应用根：Provider 装配 + 外壳布局 + 顶部导航（对话 / 观测 / FinOps）。

import { BrowserRouter, NavLink } from 'react-router-dom';
import { AppRoutes } from './routes';
import { AuthProvider } from './providers/AuthProvider';
import { QueryProvider } from './providers/QueryProvider';

const NAV = [
  { to: '/', label: '对话' },
  { to: '/observability', label: '观测' },
  { to: '/finops', label: 'FinOps' },
];

export function App() {
  return (
    <QueryProvider>
      <AuthProvider>
        <BrowserRouter>
          <div className="app-shell">
            <header className="app-header">
              <strong>数据大屏 · 多 Agent 平台</strong>
              <nav style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
                {NAV.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.to === '/'}
                    className={({ isActive }) =>
                      isActive ? 'nav-link nav-link--active' : 'nav-link'
                    }
                  >
                    {item.label}
                  </NavLink>
                ))}
              </nav>
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
