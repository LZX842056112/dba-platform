// 面板 → ECharts option 的纯函数装配（§9.5 encoding 约定）。
// 保持零 React 依赖，便于单测与复用。

import type { EChartsCoreOption } from '../../lib/echarts';
import type { Aggregate, PanelSpec, RowRecord } from '../../types/dashboard';

/** 值归一化为数字（布尔 → 1/0；数字字符串 → number；其余 null）。 */
export function numeric(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'string' && value.trim() !== '' && !Number.isNaN(Number(value))) {
    return Number(value);
  }
  return null;
}

/** 聚合一组数值。 */
export function aggregate(values: number[], agg: Aggregate | undefined): number {
  if (values.length === 0) return 0;
  switch (agg) {
    case 'avg':
      return values.reduce((acc, cur) => acc + cur, 0) / values.length;
    case 'min':
      return Math.min(...values);
    case 'max':
      return Math.max(...values);
    case 'count':
      return values.length;
    case 'none':
      return values[0] ?? 0;
    case 'sum':
    default:
      return values.reduce((acc, cur) => acc + cur, 0);
  }
}

interface SeriesDatum {
  name: string;
  type: string;
  data: number[] | Array<{ name: string; value: number }>;
  smooth?: boolean;
  stack?: string;
}

function groupByCategory(
  rows: RowRecord[],
  categories: string[],
  xField: string,
  yField: string,
  agg: Aggregate | undefined,
): number[] {
  return categories.map((category) => {
    const values = rows
      .filter((row) => String(row[xField] ?? '') === category)
      .map((row) => numeric(row[yField]) ?? 0);
    return aggregate(values, agg);
  });
}

/** 组装图表面板的 ECharts option。 */
export function buildChartOption(panel: PanelSpec, rows: RowRecord[]): EChartsCoreOption {
  const type = panel.chart?.type ?? 'line';
  const encoding = panel.encoding ?? {};
  const xField = encoding.x?.field;
  const yMeta = encoding.y ?? [];
  const seriesField = encoding.series?.field;
  const legend = panel.style?.legend ?? 'top';
  const stack = panel.style?.stack ? 'total' : undefined;

  // 饼图：name=分类，value=度量
  if (type === 'pie') {
    const nameField = xField ?? yMeta[0]?.field ?? '';
    const valueField = yMeta[0]?.field ?? xField ?? '';
    const data = rows.map((row) => ({
      name: String(row[nameField] ?? ''),
      value: numeric(row[valueField]) ?? 0,
    }));
    return {
      tooltip: { trigger: 'item' },
      legend: legend === 'none' ? undefined : { top: legend },
      series: [{ type: 'pie', radius: '62%', data }],
    };
  }

  // 仪表盘：单值
  if (type === 'gauge') {
    const value = buildMetricValue(panel, rows);
    return {
      series: [
        {
          type: 'gauge',
          progress: { show: true },
          detail: { formatter: '{value}' },
          data: [{ value, name: panel.encoding?.y?.[0]?.field ?? '' }],
        },
      ],
    };
  }

  if (!xField) {
    // 无 x 轴（退化）：按行序号出单序列
    const series: SeriesDatum[] = yMeta.map((meta) => ({
      name: meta.field,
      type,
      data: rows.map((row) => numeric(row[meta.field]) ?? 0),
      smooth: type === 'line',
      stack,
    }));
    return {
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: rows.map((_, index) => String(index + 1)) },
      yAxis: { type: 'value' },
      series,
    };
  }

  const categories = Array.from(new Set(rows.map((row) => String(row[xField] ?? ''))));
  const series: SeriesDatum[] = [];

  if (seriesField) {
    const groups = Array.from(new Set(rows.map((row) => String(row[seriesField] ?? ''))));
    for (const group of groups) {
      const subset = rows.filter((row) => String(row[seriesField] ?? '') === group);
      for (const meta of yMeta) {
        series.push({
          name: `${meta.field}·${group}`,
          type,
          data: groupByCategory(subset, categories, xField, meta.field, meta.agg),
          smooth: type === 'line',
          stack,
        });
      }
    }
  } else {
    for (const meta of yMeta) {
      series.push({
        name: meta.field,
        type,
        data: groupByCategory(rows, categories, xField, meta.field, meta.agg),
        smooth: type === 'line',
        stack,
      });
    }
  }

  return {
    tooltip: { trigger: 'axis' },
    legend: legend === 'none' ? undefined : { top: legend },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value' },
    series,
  };
}

/** 计算指标卡/仪表盘的单值。 */
export function buildMetricValue(panel: PanelSpec, rows: RowRecord[]): number {
  const first = panel.encoding?.y?.[0];
  if (!first) return 0;
  const values = rows.map((row) => numeric(row[first.field]) ?? 0);
  return aggregate(values, first.agg);
}
