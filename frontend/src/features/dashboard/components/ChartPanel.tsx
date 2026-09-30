// 图表面板：把 PanelSpec + 行数据装配为 ECharts option 并交给 EchartsBase 渲染。

import { useMemo } from 'react';
import { EchartsBase } from '../../../components/charts/EchartsBase';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { buildChartOption } from '../optionBuilder';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

export function ChartPanel({ panel, rows }: Props) {
  const option = useMemo(() => buildChartOption(panel, rows), [panel, rows]);

  if (rows.length === 0) {
    return <div className="dim">暂无数据（等待查询结果或数据源未内联）</div>;
  }

  return (
    <div style={{ flex: 1, minHeight: 0 }}>
      <EchartsBase option={option} height="100%" />
    </div>
  );
}
