// Agent↔工具拓扑（GraphChart）。

import { useQuery } from '@tanstack/react-query';
import { EchartsBase } from '../../components/charts/EchartsBase';
import { Empty, PagePanel } from '../../components/PageBits';
import { getTopology } from '../../features/observability/api';
import type { EChartsCoreOption } from '../../lib/echarts';

export function TopologyPage() {
  const { data } = useQuery({ queryKey: ['obs-topology'], queryFn: getTopology, refetchInterval: 30000 });

  const nodes = data?.nodes ?? [];
  const edges = data?.edges ?? [];

  const option: EChartsCoreOption =
    nodes.length === 0
      ? {}
      : {
          tooltip: {},
          series: [
            {
              type: 'graph',
              layout: 'force',
              roam: true,
              label: { show: true, position: 'right', color: '#e8ecf7' },
              force: { repulsion: 200, edgeLength: 80 },
              data: nodes.map((n) => ({
                name: n.name,
                symbolSize: Math.max(14, Math.min(46, (n.span_count || 0) / 2 + 10)),
                itemStyle: { color: (n.error_count || 0) > 0 ? '#ef5350' : '#22d3ee' },
              })),
              links: edges.map((e) => ({
                source: e.source,
                target: e.target,
                value: e.calls,
                lineStyle: { color: '#31406e', curveness: 0.1 },
              })),
              lineStyle: { color: '#31406e' },
            },
          ],
        };

  return (
    <PagePanel title="Agent ↔ 工具拓扑">
      {nodes.length > 0 ? (
        <div style={{ height: 480 }}>
          <EchartsBase option={option} height="100%" />
        </div>
      ) : (
        <Empty text="暂无拓扑数据（span 落库后才会出现节点）" />
      )}
    </PagePanel>
  );
}
