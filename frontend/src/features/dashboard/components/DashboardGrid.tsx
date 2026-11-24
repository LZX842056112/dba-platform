// 大屏渲染器：消费 §9.5 的 dashboard JSON。
//
// 布局策略：
//   * 有 `layout`（`dashboard.spec.ready` 已到）→ 按 12 列栅格 + items 坐标落位；
//   * 无 `layout`（delta 先到、ready 未到）→ 按面板到达顺序自动流式排布（每行 2 个），
//     保证「逐面板渲染」的感知延迟收益不被「等布局」抹掉。

import { memo, useMemo } from 'react';
import type { DataSource, Layout, PanelSpec, RowRecord } from '../../../types/dashboard';
import { PanelRenderer } from './PanelRenderer';
import { dataSourceToRows, isExternalSource } from '../selectors';

interface Props {
  panels: PanelSpec[];
  layout: Layout | null;
  dataSources: Record<string, DataSource>;
}

const EMPTY_ROWS: RowRecord[] = [];

function DashboardGridImpl({ panels, layout, dataSources }: Props) {
  const rowsByRef = useMemo(
    () => new Map(Object.entries(dataSources).map(([ref, source]) => [ref, dataSourceToRows(source)])),
    [dataSources],
  );

  if (panels.length === 0) {
    return <div className="dim">暂无面板，等待出图…</div>;
  }

  function rowsFor(panel: PanelSpec): RowRecord[] {
    const ref = panel.dataset?.ref;
    return ref ? rowsByRef.get(ref) ?? EMPTY_ROWS : EMPTY_ROWS;
  }

  /** s3 数据源：行数据不在 spec 里，需前端按预签名 URL 直拉（最小集只给入口）。 */
  function externalUrlFor(panel: PanelSpec): string | undefined {
    const ref = panel.dataset?.ref;
    const source = ref ? dataSources[ref] : undefined;
    return source?.mode === 's3' ? source.presigned_url : undefined;
  }

  function renderPanel(panel: PanelSpec) {
    const external = isExternalSource(panel, dataSources);
    return (
      <PanelRenderer
        panel={panel}
        rows={rowsFor(panel)}
        external={external}
        externalUrl={external ? externalUrlFor(panel) : undefined}
      />
    );
  }

  if (layout && layout.items.length > 0) {
    return (
      <div className="grid-12" style={{ gridAutoRows: `${layout.rowHeight}px` }}>
        {layout.items.map((item) => {
          const panel = panels.find((candidate) => candidate.panel_id === item.i);
          if (!panel) return null;
          return (
            <div
              key={item.i}
              style={{
                gridColumn: `${item.x + 1} / span ${item.w}`,
                gridRow: `${item.y + 1} / span ${item.h}`,
              }}
            >
              {renderPanel(panel)}
            </div>
          );
        })}
      </div>
    );
  }

  // 无布局：按到达顺序自动流式排布
  return (
    <div className="grid-12">
      {panels.map((panel) => (
        <div key={panel.panel_id} style={{ gridColumn: 'span 6', gridRow: 'span 8' }}>
          {renderPanel(panel)}
        </div>
      ))}
    </div>
  );
}
// 面板、布局或数据源变化时才重绘大屏。
export const DashboardGrid = memo(DashboardGridImpl);
