// 应用根：Provider 装配 + 外壳布局 + 顶部导航（对话 / 观测 / FinOps）。
//
// 信息架构采用两段式：
//   - 一级导航：对话、观测、FinOps。三个分区职责不同（实时观察 / 分析下钻 / 成本治理），不合并。
//   - 观测分区内用二级页签区分：流程监控（默认）、调用拓扑、运行明细、异常告警。

import { BrowserRouter, Link, useLocation } from 'react-router-dom';
import { AppRoutes } from './routes';
import { QueryProvider } from './providers/QueryProvider';
import { ObsSectionTabs } from './SectionTabs';

/** 一级导航项：`match` 自行判定高亮（分区的所有子路径都算命中）。 */
const NAV: Array<{ to: string; label: string; match: (pathname: string) => boolean }> = [
  { to: '/', label: '对话', match: (pathname) => pathname === '/' },
  {
    to: '/observability',
    label: '观测',
    match: (pathname) => pathname.startsWith('/observability'),
  },
  { to: '/finops', label: 'FinOps', match: (pathname) => pathname.startsWith('/finops') },
];

function NavLinks() {
  const { pathname } = useLocation();
  return (
    <nav style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
      {NAV.map((item) => {
        const active = item.match(pathname);
        return (
          <Link
            key={item.to}
            to={item.to}
            className={active ? 'nav-link nav-link--active' : 'nav-link'}
            aria-current={active ? 'page' : undefined}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function Shell() {
  const { pathname } = useLocation();
  const inObservability = pathname.startsWith('/observability');
  return (
    <div className="app-shell">
      <header className="app-header">
        <strong>数据大屏 · 多 Agent 平台</strong>
        <NavLinks />
      </header>
      {inObservability ? <ObsSectionTabs /> : null}
      <main className="app-main">
        <AppRoutes />
      </main>
    </div>
  );
}

export function App() {
  return (
    <QueryProvider>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Shell />
      </BrowserRouter>
    </QueryProvider>
  );
}
