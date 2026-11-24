// 面板渲染器：按 `panel.kind` 分派（chart | metric_card | table | text | ranking）。
//
// ★ s3 数据源（大结果未内联，§9.5 硬约定 2）走 `external` 分支：最小集不做前端直拉，
//   但必须明确告知原因并给出可打开的预签名 URL，不能退化成一句「暂无数据」。

import { memo, type ReactElement } from 'react';
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
  /** 数据源为 s3（结果未内联）。 */
  external?: boolean;
  /** s3 数据源的预签名直拉地址（可能为空）。 */
  externalUrl?: string;
}

export const PanelRenderer = memo(function PanelRenderer({
  panel,
  rows,
  external,
  externalUrl,
}: Props) {
  const body = ((): ReactElement => {
    if (external) {
      return (
        <div className="dim" style={{ fontSize: 12, lineHeight: 1.6 }}>
          该面板结果未内联（data_source.mode = s3）。
          {externalUrl ? (
            <>
              {' '}
              <a href={externalUrl} target="_blank" rel="noreferrer">
                打开结果文件（预签名 URL）
              </a>
            </>
          ) : (
            ' 未提供预签名 URL，前端直拉未实现。'
          )}
        </div>
      );
    }
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
});
