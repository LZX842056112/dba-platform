// 路由表：登录 + 对话页 + 观测 5 页 + FinOps 4 页。

import { Navigate, Route, Routes } from 'react-router-dom';
import { ChatSessionPage } from '../pages/chatbi/ChatSessionPage';
import { LoginPage } from '../pages/login/LoginPage';
import { AnomaliesPage } from '../pages/observability/AnomaliesPage';
import { OverviewPage } from '../pages/observability/OverviewPage';
import { RunDetailPage } from '../pages/observability/RunDetailPage';
import { RunsPage } from '../pages/observability/RunsPage';
import { TopologyPage } from '../pages/observability/TopologyPage';
import { BudgetPage } from '../pages/finops/BudgetPage';
import { CostOverviewPage } from '../pages/finops/CostOverviewPage';
import { RecommendationsPage } from '../pages/finops/RecommendationsPage';
import { ReuseCurvePage } from '../pages/finops/ReuseCurvePage';
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
  );
}
