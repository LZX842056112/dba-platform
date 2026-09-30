// 指标卡（metric_card）：单值 + 单位（§9.5 encoding.y[0] + style.unit）。

import { useMemo } from 'react';
import { formatNumber } from '../../../lib/format';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { buildMetricValue } from '../optionBuilder';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

export function MetricCard({ panel, rows }: Props) {
  const value = useMemo(() => buildMetricValue(panel, rows), [panel, rows]);
  const unit = panel.style?.unit ?? '';

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'flex-start',
      }}
    >
      <div className="metric-value">
        {formatNumber(value)}
        {unit ? <span style={{ fontSize: 16, marginLeft: 4 }}>{unit}</span> : null}
      </div>
      {panel.subtitle ? <div className="dim panel-card__sub">{panel.subtitle}</div> : null}
    </div>
  );
}
