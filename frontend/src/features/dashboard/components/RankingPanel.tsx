// 排行榜面板（kind="ranking"）：TopN + 进度条，纯 HTML/CSS，零图表依赖。
//
// 视觉对照参考图的「扫码地区 Top5 / 订单数据分析」：名次 + 名称 + 数值 + 进度条。

import { useMemo } from 'react';
import { formatNumber } from '../../../lib/format';
import type { PanelSpec, RowRecord } from '../../../types/dashboard';
import { numeric } from '../optionBuilder';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

interface Item {
  name: string;
  value: number;
}

export function RankingPanel({ panel, rows }: Props) {
  const { items, max } = useMemo(() => {
    const encoding = panel.encoding ?? {};
    const nameField = encoding.x?.field ?? encoding.y?.[0]?.field ?? '';
    const valueField = encoding.y?.[0]?.field ?? encoding.x?.field ?? '';
    const sorted: Item[] = rows
      .map((row) => ({
        name: String(row[nameField] ?? ''),
        value: numeric(row[valueField]) ?? 0,
      }))
      .sort((a, b) => (panel.style?.sort === 'asc' ? a.value - b.value : b.value - a.value));
    const topN = panel.style?.topN && panel.style.topN > 0 ? panel.style.topN : sorted.length;
    const trimmed = sorted.slice(0, topN);
    return { items: trimmed, max: Math.max(1, ...trimmed.map((i) => i.value)) };
  }, [panel, rows]);

  if (items.length === 0) {
    return <div className="dim">暂无数据</div>;
  }

  return (
    <div className="rank-list">
      {items.map((item, index) => (
        <div className="rank-row" key={`${panel.panel_id}-${item.name}-${index}`}>
          <span className={`rank-no${index < 3 ? ' rank-no--top' : ''}`}>{index + 1}</span>
          <span className="rank-name" title={item.name}>
            {item.name}
          </span>
          <span className="rank-bar">
            <span className="rank-bar__fill" style={{ width: `${(item.value / max) * 100}%` }} />
          </span>
          <span className="rank-value">{formatNumber(item.value)}</span>
        </div>
      ))}
    </div>
  );
}
