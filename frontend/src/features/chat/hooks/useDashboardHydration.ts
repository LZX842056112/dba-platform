// 大屏数据补全：面板经 SSE 只带来 `dataset.ref`，真正的行数据在 data_sources 里（§9.5）。
// `dashboard.spec.ready` 到达后，按 dashboard_id 回拉整份大屏 JSON 并合并进 store。

import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { getDashboardSpec } from '../api/chatApi';
import { useRunStore } from '../store/runStore';

export function useDashboardHydration(): void {
  const dashboardId = useRunStore((state) => state.dashboardId);
  const hydrateDashboard = useRunStore((state) => state.hydrateDashboard);

  const { data } = useQuery({
    queryKey: ['dashboard', dashboardId],
    enabled: Boolean(dashboardId),
    queryFn: () => getDashboardSpec(dashboardId as string),
    staleTime: 30_000,
  });

  useEffect(() => {
    if (data) hydrateDashboard(data);
  }, [data, hydrateDashboard]);
}
