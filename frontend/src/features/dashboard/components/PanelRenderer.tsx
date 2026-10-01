// 面板渲染器：按 `panel.kind` 分派（chart | metric_card | table | text | ranking）。

import type { ReactElement } from 'react';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { ChartPanel } from './ChartPanel';
import { DataTable } from './DataTable';
import { MetricCard } from './MetricCard';
import { PanelErrorBoundary } from './PanelErrorBoundary';
import { RankingPanel } from './RankingPanel';
import { TextPanel } from './TextPanel';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

export function PanelRenderer({ panel, rows }: Props) {
  const body = ((): ReactElement => {
    switch (panel.kind) {
      case 'chart':
        return <ChartPanel panel={panel} rows={rows} />;
      case 'metric_card':
        return <MetricCard panel={panel} rows={rows} />;
      case 'ranking':
        return <RankingPanel panel={panel} rows={rows} />;
      case 'table':
        return <DataTable panel={panel} rows={rows} />;
      case 'text':
        return <TextPanel panel={panel} />;
      default:
        // 未知 kind：带了 chart 配置就尽力出图，否则友好降级（不再显示生硬的报错）
        return panel.chart ? (
          <ChartPanel panel={panel} rows={rows} />
        ) : (
          <div className="dim">暂不支持的面板类型：{String(panel.kind)}</div>
        );
    }
  })();

  return (
    <div className="panel-card panel-card--neon" style={{ height: '100%' }}>
      <div className="panel-card__head">
        <span className="panel-title" style={{ marginBottom: 0 }}>
          {panel.title ?? panel.panel_id}
        </span>
        {panel.subtitle ? <span className="dim panel-card__sub">{panel.subtitle}</span> : null}
      </div>
      <PanelErrorBoundary panelId={panel.panel_id}>{body}</PanelErrorBoundary>
    </div>
  );
}
