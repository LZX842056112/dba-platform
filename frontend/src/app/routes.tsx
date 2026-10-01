// 路由表：登录 + 对话页 + 观测 5 页 + FinOps 4 页。
// ★ 除登录页外全部路由级懒加载（React.lazy + Suspense），按需拆包。

import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { LoginPage } from '../pages/login/LoginPage';
import { RequireAuth } from './providers/AuthProvider';

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
  return (
    <Suspense fallback={<Fallback />}>
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
        <Route path="/observability" element={<RequireAuth><OverviewPage /></RequireAuth>} />
        <Route path="/observability/topology" element={<RequireAuth><TopologyPage /></RequireAuth>} />
        <Route path="/observability/runs" element={<RequireAuth><RunsPage /></RequireAuth>} />
        <Route path="/observability/runs/:traceId" element={<RequireAuth><RunDetailPage /></RequireAuth>} />
        <Route path="/observability/anomalies" element={<RequireAuth><AnomaliesPage /></RequireAuth>} />
        <Route path="/finops" element={<RequireAuth><CostOverviewPage /></RequireAuth>} />
        <Route path="/finops/reuse" element={<RequireAuth><ReuseCurvePage /></RequireAuth>} />
        <Route path="/finops/budgets" element={<RequireAuth><BudgetPage /></RequireAuth>} />
        <Route path="/finops/recommendations" element={<RequireAuth><RecommendationsPage /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
