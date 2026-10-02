// 路由表：登录 + 对话页 + 观测 5 页 + FinOps 4 页。
// ★ 除登录页外全部路由级懒加载（React.lazy + Suspense），按需拆包。

import { lazy, Suspense } from 'react';
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { LoginPage } from '../pages/login/LoginPage';
import { RequireAuth } from './providers/AuthProvider';
import { RouteErrorBoundary } from './RouteErrorBoundary';

const ChatSessionPage = lazy(() =>
  import('../pages/chatbi/ChatSessionPage').then((m) => ({ default: m.ChatSessionPage })),
);
const OverviewPage = lazy(() =>
  import('../pages/observability/OverviewPage').then((m) => ({ default: m.OverviewPage })),
);
const TopologyPage = lazy(() =>
  import('../pages/observability/TopologyPage').then((m) => ({ default: m.TopologyPage })),
);
const RunsPage = lazy(() =>
  import('../pages/observability/RunsPage').then((m) => ({ default: m.RunsPage })),
);
const RunDetailPage = lazy(() =>
  import('../pages/observability/RunDetailPage').then((m) => ({ default: m.RunDetailPage })),
);
const AnomaliesPage = lazy(() =>
  import('../pages/observability/AnomaliesPage').then((m) => ({ default: m.AnomaliesPage })),
);
const CostOverviewPage = lazy(() =>
  import('../pages/finops/CostOverviewPage').then((m) => ({ default: m.CostOverviewPage })),
);
const ReuseCurvePage = lazy(() =>
  import('../pages/finops/ReuseCurvePage').then((m) => ({ default: m.ReuseCurvePage })),
);
const BudgetPage = lazy(() =>
  import('../pages/finops/BudgetPage').then((m) => ({ default: m.BudgetPage })),
);
const RecommendationsPage = lazy(() =>
  import('../pages/finops/RecommendationsPage').then((m) => ({ default: m.RecommendationsPage })),
);

function Fallback() {
  return <div style={{ padding: 24, color: 'var(--color-dim, #888)' }}>加载中…</div>;
}

export function AppRoutes() {
  const location = useLocation();
  return (
    <Suspense fallback={<Fallback />}>
      <RouteErrorBoundary key={location.pathname}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<RequireAuth><Outlet /></RequireAuth>}>
            <Route path="/" element={<ChatSessionPage />} />
            <Route path="/observability" element={<OverviewPage />} />
            <Route path="/observability/topology" element={<TopologyPage />} />
            <Route path="/observability/runs" element={<RunsPage />} />
            <Route path="/observability/runs/:traceId" element={<RunDetailPage />} />
            <Route path="/observability/anomalies" element={<AnomaliesPage />} />
            <Route path="/finops" element={<CostOverviewPage />} />
            <Route path="/finops/reuse" element={<ReuseCurvePage />} />
            <Route path="/finops/budgets" element={<BudgetPage />} />
            <Route path="/finops/recommendations" element={<RecommendationsPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </RouteErrorBoundary>
    </Suspense>
  );
}
