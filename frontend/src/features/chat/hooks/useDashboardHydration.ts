// 大屏数据补全：面板经 SSE 只带来 `dataset.ref`，真正的行数据在 data_sources 里（§9.5）。
//
// ★ 门槛是 `status === 'done'`（收到 run.finished）而不是 `dashboard.spec.ready`：
//   spec 由后端在流水线结束后、**发 run.finished 之前**落库；若在 spec.ready 时
//   就回拉，会抢在落库前请求 → 拿到空 spec → 图表恒「暂无数据」（实测竞态）。
//
// ★ 结构校验：§9.5 的 schema_version / 12 列 / panel.kind / data_sources.mode 由
//   lib/schema 的 validateDashboardSpec 负责。校验不通过时**仍然渲染**（尽量让用户
//   看到部分内容），但必须给出可见横幅——否则这条约定只是注释里的承诺。

import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { validateDashboardSpec } from '../../../lib/schema';
import { getDashboardSpec } from '../api/chatApi';
import { useRunStore } from '../store/runStore';

export function useDashboardHydration(): void {
  const dashboardId = useRunStore((state) => state.dashboardId);
  const status = useRunStore((state) => state.status);
  const hydrateDashboard = useRunStore((state) => state.hydrateDashboard);
  const setBanner = useRunStore((state) => state.setBanner);

  const { data } = useQuery({
    queryKey: ['dashboard', dashboardId],
    enabled: Boolean(dashboardId) && status === 'done',
    queryFn: () => getDashboardSpec(dashboardId as string),
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!data) return;
    const result = validateDashboardSpec(data);
    if (!result.ok) {
      console.error('[dba] 大屏结构校验未通过', result.errors);
      setBanner({ kind: 'error', text: `大屏结构校验未通过：${result.errors[0]}` });
    }
    hydrateDashboard(data);
  }, [data, hydrateDashboard, setBanner]);
}
