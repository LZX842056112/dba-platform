// 观测分区页签（流程监控 / 调用拓扑 / 运行明细 / 异常告警）。
//
// 为什么放外壳层：这组页签是「观测」分区的二级导航，不是页面内容。
// 由 App 在 pathname 落在 /observability 时渲染一次，避免每个页面各写一遍。

import { Link, useLocation } from 'react-router-dom';

const TABS: Array<{ to: string; label: string; match: (pathname: string) => boolean }> = [
  {
    to: '/observability/flow',
    label: '流程监控',
    // /observability 会重定向到本页，分区根路径也归到这一档
    match: (pathname) => pathname === '/observability' || pathname.startsWith('/observability/flow'),
  },
  {
    to: '/observability/topology',
    label: '调用拓扑',
    match: (pathname) => pathname.startsWith('/observability/topology'),
  },
  {
    to: '/observability/runs',
    label: '运行明细',
    match: (pathname) => pathname.startsWith('/observability/runs'),
  },
  {
    to: '/observability/anomalies',
    label: '异常告警',
    match: (pathname) => pathname.startsWith('/observability/anomalies'),
  },
];

export function ObsSectionTabs() {
  const { pathname } = useLocation();
  return (
    <nav className="section-tabs" aria-label="观测分区">
      {TABS.map((tab) => {
        const active = tab.match(pathname);
        return (
          <Link
            key={tab.to}
            to={tab.to}
            className={active ? 'section-tab section-tab--active' : 'section-tab'}
            aria-current={active ? 'page' : undefined}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
