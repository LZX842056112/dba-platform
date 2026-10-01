// 指标卡（metric_card）：单值 + 单位（§9.5 encoding.y[0] + style.unit）。
//
// ★ style.variant === 'flip' 时启用「数字翻牌」动画（参考图顶部的大数字 KPI）。

import { useMemo } from 'react';
import { formatNumber } from '../../../lib/format';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { useCountUp } from '../hooks/useCountUp';
import { buildMetricValue } from '../optionBuilder';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

export function MetricCard({ panel, rows }: Props) {
  const value = useMemo(() => buildMetricValue(panel, rows), [panel, rows]);
  const unit = panel.style?.unit ?? '';
  const flip = panel.style?.variant === 'flip' || panel.style?.animate === true;
  const animated = useCountUp(value, 900, flip);
  const trend = panel.style?.trend;
  const trendValue = panel.style?.trendValue;

  return (
    <div className="metric-card">
      <div className={`metric-value${flip ? ' metric-value--flip' : ''}`}>
        {formatNumber(flip ? animated : value)}
        {unit ? <span className="metric-unit">{unit}</span> : null}
      </div>
      {trend ? (
        <div className={`metric-trend metric-trend--${trend}`}>
          {trend === 'up' ? '▲' : trend === 'down' ? '▼' : '—'}
          {typeof trendValue === 'number' ? ` ${trendValue}%` : ''}
        </div>
      ) : null}
      {panel.subtitle ? <div className="dim panel-card__sub">{panel.subtitle}</div> : null}
    </div>
  );
}
