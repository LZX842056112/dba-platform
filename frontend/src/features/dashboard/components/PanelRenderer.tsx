// 面板渲染器：按 `panel.kind` 分派（§9.5：chart | metric_card | table | text）。

import type { ReactElement } from 'react';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { ChartPanel } from './ChartPanel';
import { DataTable } from './DataTable';
import { MetricCard } from './MetricCard';
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
      case 'table':
        return <DataTable panel={panel} rows={rows} />;
      case 'text':
        return <TextPanel panel={panel} />;
      default:
        return <div className="dim">不支持的 panel.kind：{String(panel.kind)}</div>;
    }
  })();

  return (
    <div className="panel-card" style={{ height: '100%' }}>
      <div className="panel-card__head">
        <span className="panel-title" style={{ marginBottom: 0 }}>
          {panel.title ?? panel.panel_id}
        </span>
        {panel.subtitle ? <span className="dim panel-card__sub">{panel.subtitle}</span> : null}
      </div>
      {body}
    </div>
  );
}
