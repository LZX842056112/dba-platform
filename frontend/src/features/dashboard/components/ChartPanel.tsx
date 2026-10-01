// 图表面板：把 PanelSpec + 行数据装配为 ECharts option 并交给 EchartsBase 渲染。
//
// ★ 地图需先 `registerMap`，故 `map` 类型要等 GeoJSON 懒加载完成再渲染
//   （582KB 只在真的出现地图面板时才拉取）。

import { useEffect, useMemo, useState } from 'react';
import { EchartsBase } from '../../../components/charts/EchartsBase';
import { ensureChinaMap } from '../../../lib/chinaMap';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { buildChartOption } from '../optionBuilder';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

export function ChartPanel({ panel, rows }: Props) {
  const isMap = panel.chart?.type === 'map';
  const [mapReady, setMapReady] = useState(!isMap);

  useEffect(() => {
    if (!isMap) {
      setMapReady(true);
      return;
    }
    let cancelled = false;
    ensureChinaMap()
      .then(() => {
        if (!cancelled) setMapReady(true);
      })
      .catch(() => {
        if (!cancelled) setMapReady(true); // 注册失败也放行，由 ECharts 报错可见
      });
    return () => {
      cancelled = true;
    };
  }, [isMap]);

  const option = useMemo(() => buildChartOption(panel, rows), [panel, rows]);

  if (rows.length === 0) {
    return <div className="dim">暂无数据（等待查询结果或数据源未内联）</div>;
  }
  if (!mapReady) {
    return <div className="dim">地图数据加载中…</div>;
  }

  return (
    <div style={{ flex: 1, minHeight: 0 }}>
      <EchartsBase option={option} height="100%" />
    </div>
  );
}
