// 表格面板（table）：按行记录动态取列，最多渲染前 100 行。

import type { PanelSpec, RowRecord } from '../../../types/dashboard';

interface Props {
  panel: PanelSpec;
  rows: RowRecord[];
}

const MAX_ROWS = 100;

function renderCell(value: string | number | boolean | null): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

export function DataTable({ panel, rows }: Props) {
  if (rows.length === 0) {
    return <div className="dim">暂无数据</div>;
  }
  const columns = Object.keys(rows[0]);
  const shown = rows.slice(0, MAX_ROWS);

  return (
    <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col}>{col}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, index) => (
            <tr key={`${panel.panel_id}-${index}`}>
              {columns.map((col) => (
                <td key={col}>{renderCell(row[col])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > MAX_ROWS ? (
        <div className="dim panel-card__sub">仅显示前 {MAX_ROWS} 行，共 {rows.length} 行</div>
      ) : null}
    </div>
  );
}
