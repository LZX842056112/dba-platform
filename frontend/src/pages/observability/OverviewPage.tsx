// 观测总览：四问 KPI 卡 + Top Agent + 平台自身成本。

import { useQuery } from '@tanstack/react-query';
import { Empty, PagePanel, SimpleList, StatCard } from '../../components/PageBits';
import { getOverview, getSelfCost } from '../../features/observability/api';
import { formatNumber } from '../../lib/format';

export function OverviewPage() {
  const { data } = useQuery({ queryKey: ['obs-overview'], queryFn: getOverview, refetchInterval: 15000 });
  const { data: selfCost } = useQuery({ queryKey: ['obs-self'], queryFn: getSelfCost, refetchInterval: 30000 });
  const kpi = data?.kpi_cards;
  const topAgents = (data?.top_agents ?? []) as Array<Record<string, unknown>>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
        <StatCard label="正在运行" value={kpi?.running_now ?? 0} />
        <StatCard label="今日成本" value={formatNumber(kpi?.cost_today_micro ?? 0)} unit=" µ$" />
        <StatCard label="成功率" value={kpi?.success_rate ?? 0} unit="%" />
        <StatCard label="静默失败" value={kpi?.silent_failures ?? 0} />
      </div>
      <PagePanel title="Top Agent（按调用量）">
        <SimpleList items={topAgents} emptyText="暂无 Agent 数据（多问几次积累 Run）" />
      </PagePanel>
      <PagePanel title="平台自身成本（不含业务流量）">
        {selfCost && selfCost.total_runs > 0 ? (
          <SimpleList
            items={Object.entries(selfCost.by_module).map(([module, v]) => ({
              module,
              cost_micro_usd: v.cost_micro_usd,
              runs: v.runs,
            }))}
          />
        ) : (
          <Empty text="暂无平台自身成本数据" />
        )}
      </PagePanel>
    </div>
  );
}
