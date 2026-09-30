// 大屏渲染器：消费 §9.5 的 dashboard JSON。
//
// 布局策略：
//   * 有 `layout`（`dashboard.spec.ready` 已到）→ 按 12 列栅格 + items 坐标落位；
//   * 无 `layout`（delta 先到、ready 未到）→ 按面板到达顺序自动流式排布（每行 2 个），
//     保证「逐面板渲染」的感知延迟收益不被「等布局」抹掉。

import type { DataSource, Layout, PanelSpec } from '../../../types/dashboard';
import { PanelRenderer } from './PanelRenderer';
import { resolvePanelRows } from '../selectors';

interface Props {
  panels: PanelSpec[];
  layout: Layout | null;
  dataSources: Record<string, DataSource>;
}

export function DashboardGrid({ panels, layout, dataSources }: Props) {
  if (panels.length === 0) {
    return <div className="dim">暂无面板，等待出图…</div>;
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
              <PanelRenderer panel={panel} rows={resolvePanelRows(panel, dataSources)} />
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
          <PanelRenderer panel={panel} rows={resolvePanelRows(panel, dataSources)} />
        </div>
      ))}
    </div>
  );
}
