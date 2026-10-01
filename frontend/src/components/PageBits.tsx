// 观测/FinOps 页面共享小件：页面壳、KPI 卡、空态、简单列表。

import type { ReactNode } from 'react';

/** 页面区块壳（统一标题 + 内容，复用现有 .panel/.panel-title 样式）。 */
export function PagePanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-title">{title}</div>
      {children}
    </section>
  );
}

/** 指标卡（label + 大数字 + 单位）。 */
export function StatCard({
  label,
  value,
  unit = '',
  hint,
}: {
  label: string;
  value: string | number;
  unit?: string;
  hint?: string;
}) {
  return (
    <div className="panel-card panel-card--neon" style={{ padding: 14 }}>
      <div className="dim" style={{ fontSize: 12 }}>
        {label}
      </div>
      <div className="metric-value" style={{ fontSize: 26, lineHeight: 1.4 }}>
        {value}
        {unit ? <span className="metric-unit">{unit}</span> : null}
      </div>
      {hint ? <div className="dim" style={{ fontSize: 11 }}>{hint}</div> : null}
    </div>
  );
}

/** 诚实空态。 */
export function Empty({ text = '暂无数据' }: { text?: string }) {
  return <div className="dim" style={{ padding: '16px 4px' }}>{text}</div>;
}

/** 简单键值列表（无分页）。 */
export function SimpleList({
  items,
  emptyText = '暂无数据',
}: {
  items: Array<Record<string, unknown>>;
  emptyText?: string;
}) {
  if (items.length === 0) return <Empty text={emptyText} />;
  const columns = Object.keys(items[0]).slice(0, 8);
  return (
    <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c}>{String(row[c] ?? '—')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
