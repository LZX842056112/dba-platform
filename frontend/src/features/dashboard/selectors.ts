// 大屏数据装配：把 `data_sources`（§9.5）展开为「列名 → 值」的行记录，
// 并解析面板的 `dataset.ref` 指向。行内只保留最小集所需逻辑。

import type { DataSource, PanelSpec, RowRecord } from '../../types/dashboard';

/** 展开单个数据源为行记录数组（仅 inline；s3 需前端直拉，最小集只标注）。 */
export function dataSourceToRows(source: DataSource | undefined): RowRecord[] {
  if (!source) return [];
  if (source.mode !== 'inline') return [];
  const names = source.columns.map((col) => col.name);
  return source.rows.map((row) => {
    const record: RowRecord = {};
    names.forEach((name, index) => {
      record[name] = row[index] ?? null;
    });
    return record;
  });
}

/** 解析面板行数据：按 `panel.dataset.ref` 从数据源字典取行。 */
export function resolvePanelRows(
  panel: PanelSpec,
  dataSources: Record<string, DataSource>,
): RowRecord[] {
  const ref = panel.dataset?.ref;
  if (!ref) return [];
  return dataSourceToRows(dataSources[ref]);
}

/** 数据源是否为「大结果未内联」（需前端走预签名 URL 直拉，§9.5 硬约定 2）。 */
export function isExternalSource(panel: PanelSpec, dataSources: Record<string, DataSource>): boolean {
  const ref = panel.dataset?.ref;
  if (!ref) return false;
  return dataSources[ref]?.mode === 's3';
}
