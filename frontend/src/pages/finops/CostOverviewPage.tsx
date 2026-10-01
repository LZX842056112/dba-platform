// FinOps 成本总览：总量 + 模型分解环图 + TopN 花钱方 + 计价覆盖率。

import { useQuery } from '@tanstack/react-query';
import { EchartsBase } from '../../components/charts/EchartsBase';
import { Empty, PagePanel, StatCard } from '../../components/PageBits';
import { gaugeOption, pieOption } from '../../features/dashboard/simpleCharts';
import { getCostSummary, getCoverage, getTopSpenders } from '../../features/finops/api';
import { formatMicroUsd } from '../../lib/format';

export function CostOverviewPage() {
  const summary = useQuery({ queryKey: ['finops-summary'], queryFn: getCostSummary, refetchInterval: 30000 });
  const coverage = useQuery({ queryKey: ['finops-coverage'], queryFn: getCoverage, refetchInterval: 30000 });
  const top = useQuery({ queryKey: ['finops-top'], queryFn: getTopSpenders, refetchInterval: 30000 });

  const byModel = Object.entries(summary.data?.by_model ?? {}).map(([name, value]) => ({ name, value }));
  const topSpenders = top.data ?? [];
  const pricedRatio = Math.round((coverage.data?.priced_ratio ?? 0) * 100);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
        <StatCard label="累计成本" value={formatMicroUsd(summary.data?.total ?? 0)} />
        <StatCard label="计价覆盖率" value={pricedRatio} unit="%" />
        <StatCard label="未计价调用" value={coverage.data?.unpriced_calls ?? 0} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <PagePanel title="按模型成本分解">
          {byModel.length ? (
            <div style={{ height: 280 }}><EchartsBase option={pieOption(byModel)} height="100%" /></div>
          ) : <Empty />}
        </PagePanel>
        <PagePanel title="计价覆盖率">
          <div style={{ height: 280 }}><EchartsBase option={gaugeOption(pricedRatio, 100, '%')} height="100%" /></div>
        </PagePanel>
      </div>
      <PagePanel title="Top 花钱方">
        {topSpenders.length ? (
          <table className="data-table">
            <thead><tr><th>#</th><th>维度</th><th>成本</th></tr></thead>
            <tbody>
              {topSpenders.map((s, i) => (
                <tr key={i}><td>{i + 1}</td><td>{s.key}</td><td>{formatMicroUsd(s.cost_micro_usd)}</td></tr>
              ))}
            </tbody>
          </table>
        ) : <Empty />}
      </PagePanel>
    </div>
  );
}
