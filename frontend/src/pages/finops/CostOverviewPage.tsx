// FinOps 成本总览：总量 + 模型分解环图 + TopN 花钱方 + 计价覆盖率 + 平台自身成本。
//
// ★ 「平台自身成本」原在观测总览页（已删除，其内容由流程监控覆盖）。
//   该块口径属于成本归属，故迁到本页；它不计入上面的业务成本曲线。

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { EchartsBase } from '../../components/charts/EchartsBase';
import { Empty, PagePanel, ReportingWindow, StatCard } from '../../components/PageBits';
import { gaugeOption, pieOption } from '../../features/dashboard/simpleCharts';
import { getCostSummary, getCoverage, getTopSpenders } from '../../features/finops/api';
import { getSelfCost } from '../../features/observability/api';
import { formatMicroUsd, formatNumber } from '../../lib/format';

export function CostOverviewPage() {
  const [hours, setHours] = useState(30 * 24);
  const summary = useQuery({ queryKey: ['finops-summary', hours], queryFn: () => getCostSummary(hours), refetchInterval: 30000 });
  const coverage = useQuery({ queryKey: ['finops-coverage', hours], queryFn: () => getCoverage(hours), refetchInterval: 30000 });
  const top = useQuery({ queryKey: ['finops-top', hours], queryFn: () => getTopSpenders(hours), refetchInterval: 30000 });
  const self = useQuery({ queryKey: ['obs-self', hours], queryFn: () => getSelfCost(hours), refetchInterval: 30000 });

  const byModel = Object.entries(summary.data?.by_model ?? {})
    .filter(([, value]) => value > 0)
    .map(([name, value]) => ({ name: name || '未记录模型', value }));
  const topSpenders = (top.data ?? []).filter((spender) => spender.cost_micro_usd > 0);
  const totalCalls = coverage.data?.total_calls;
  const pricedRatio = Math.round((coverage.data?.priced_ratio ?? 0) * 100);

  const selfCost = self.data;
  const selfModules = Object.entries(selfCost?.by_module ?? {}).sort(
    (a, b) => b[1].cost_micro_usd - a[1].cost_micro_usd,
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <ReportingWindow hours={hours} onChange={setHours} />
      {(summary.isError || coverage.isError || top.isError || self.isError) && <div role="alert">部分 FinOps 指标读取失败。</div>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 8 }}>
        <StatCard label="区间成本" value={formatMicroUsd(summary.data?.total)} />
        <StatCard label="模型调用" value={formatNumber(totalCalls)} />
        <StatCard label="计价覆盖率" value={totalCalls ? `${pricedRatio}%` : '—'} />
        <StatCard label="未计价调用" value={formatNumber(coverage.data?.unpriced_calls)} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <PagePanel title="按模型成本分解">
          {byModel.length ? (
            <div style={{ height: 280 }}><EchartsBase option={pieOption(byModel)} height="100%" /></div>
          ) : <Empty text={summary.isPending ? '正在加载…' : '所选区间没有已汇总的模型成本。'} />}
        </PagePanel>
        <PagePanel title="计价覆盖率">
          {totalCalls ? (
            <div style={{ height: 280 }}><EchartsBase option={gaugeOption(pricedRatio, 100, '%')} height="100%" /></div>
          ) : <Empty text={coverage.isPending ? '正在加载…' : '所选区间没有模型调用记录。'} />}
        </PagePanel>
      </div>
      <PagePanel title="Top 花钱方">
        {topSpenders.length ? (
          <table className="data-table">
            <thead><tr><th>#</th><th>维度</th><th>成本</th></tr></thead>
            <tbody>
              {topSpenders.map((s, i) => (
                <tr key={s.key}><td>{i + 1}</td><td>{s.key || '未记录模型'}</td><td>{formatMicroUsd(s.cost_micro_usd)}</td></tr>
              ))}
            </tbody>
          </table>
        ) : <Empty text={top.isPending ? '正在加载…' : '所选区间没有已汇总的排名。'} />}
      </PagePanel>
      <PagePanel title="平台自身成本（不含业务流量）">
        {selfModules.length ? (
          <>
            <table className="data-table">
              <thead>
                <tr><th>模块</th><th>成本</th><th>Run 数</th></tr>
              </thead>
              <tbody>
                {selfModules.map(([module, stat]) => (
                  <tr key={module}>
                    <td>{module}</td>
                    <td>{formatMicroUsd(stat.cost_micro_usd)}</td>
                    <td>{formatNumber(stat.runs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="dim" style={{ margin: '6px 0 0', fontSize: 11 }}>
              合计 {formatMicroUsd(selfCost?.total_cost_micro_usd)} / {formatNumber(selfCost?.total_runs)} 次运行。
              该部分（观测、FinOps 自身开销）已从上面的业务成本中排除。
            </p>
          </>
        ) : (
          <Empty text={self.isPending ? '正在加载…' : '所选区间没有平台自身运行记录。'} />
        )}
      </PagePanel>
    </div>
  );
}
