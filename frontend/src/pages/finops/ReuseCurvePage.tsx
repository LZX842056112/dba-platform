// 复用率 vs token 曲线（FinOps 的「差异化证据」）。

import { useQuery } from '@tanstack/react-query';
import { EchartsBase } from '../../components/charts/EchartsBase';
import { Empty, PagePanel, StatCard } from '../../components/PageBits';
import { lineOption } from '../../features/dashboard/simpleCharts';
import { getReuseCurve } from '../../features/finops/api';
import { formatMicroUsd } from '../../lib/format';

export function ReuseCurvePage() {
  const { data } = useQuery({ queryKey: ['finops-curve'], queryFn: getReuseCurve, refetchInterval: 30000 });

  const reuseSeries = (data?.reuse_series ?? []) as Array<{ date?: string; reuse_rate?: number }>;
  const tokenSeries = (data?.token_series ?? []) as Array<{
    date?: string;
    avg_tokens_per_task?: number;
    group?: string;
  }>;

  const dates = reuseSeries.map((p) => p.date ?? '');
  const reuseRates = reuseSeries.map((p) => p.reuse_rate ?? 0);

  const tokenDates = Array.from(new Set(tokenSeries.map((p) => p.date ?? '')));
  const groups = Array.from(new Set(tokenSeries.map((p) => p.group ?? 'all')));
  const tokenLines = groups.map((g) => ({
    name: g,
    data: tokenDates.map(
      (d) => tokenSeries.find((p) => p.date === d && (p.group ?? 'all') === g)?.avg_tokens_per_task ?? 0,
    ),
  }));

  if (data?.note && reuseSeries.length === 0) {
    return <PagePanel title="复用率 vs Token"><Empty text={data.note} /></PagePanel>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}>
        <StatCard label="复用率-成本相关系数" value={data?.correlation ?? '—'} />
        <StatCard label="累计节省" value={formatMicroUsd(data?.total_saved_micro_usd ?? 0)} />
      </div>
      <PagePanel title="技能复用率（按日）">
        {dates.length ? (
          <div style={{ height: 260 }}><EchartsBase option={lineOption(dates, [{ name: '复用率', data: reuseRates }])} height="100%" /></div>
        ) : <Empty />}
      </PagePanel>
      <PagePanel title="每任务平均 Token（含对照组）">
        {tokenDates.length ? (
          <div style={{ height: 260 }}><EchartsBase option={lineOption(tokenDates, tokenLines)} height="100%" /></div>
        ) : <Empty />}
      </PagePanel>
    </div>
  );
}
