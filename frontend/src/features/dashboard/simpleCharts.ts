// 观测/FinOps 页面的简版图表 option 装配（直接吃原始数据，不经过 PanelSpec）。
// 深色观感由全局主题 dba-dark 承担，这里只负责形态。

import type { EChartsCoreOption } from '../../lib/echarts';

export function lineOption(
  xs: string[],
  series: Array<{ name: string; data: number[] }>,
): EChartsCoreOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: series.length > 1 ? { top: 0 } : undefined,
    grid: { left: 8, right: 16, top: series.length > 1 ? 32 : 16, bottom: 8, containLabel: true },
    xAxis: { type: 'category', data: xs },
    yAxis: { type: 'value' },
    series: series.map((s) => ({
      name: s.name,
      type: 'line',
      data: s.data,
      smooth: true,
      areaStyle: series.length === 1 ? { opacity: 0.2 } : undefined,
    })),
  };
}

export function pieOption(items: Array<{ name: string; value: number }>): EChartsCoreOption {
  return {
    tooltip: { trigger: 'item' },
    legend: { top: 'bottom' },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        itemStyle: { borderColor: '#0b1020', borderWidth: 2, borderRadius: 4 },
        label: { color: '#9aa4c4', formatter: '{b} {d}%' },
        data: items,
      },
    ],
  };
}

export function barOption(
  xs: string[],
  series: Array<{ name: string; data: number[] }>,
): EChartsCoreOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: series.length > 1 ? { top: 0 } : undefined,
    grid: { left: 8, right: 16, top: series.length > 1 ? 32 : 16, bottom: 8, containLabel: true },
    xAxis: { type: 'category', data: xs },
    yAxis: { type: 'value' },
    series: series.map((s) => ({ name: s.name, type: 'bar', data: s.data })),
  };
}

export function gaugeOption(value: number, max: number, unit = ''): EChartsCoreOption {
  return {
    series: [
      {
        type: 'gauge',
        min: 0,
        max,
        progress: { show: true, width: 10, roundCap: true },
        axisLine: { lineStyle: { width: 10, color: [[1, 'rgba(42,51,85,0.6)']] } },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: '#2a3355' } },
        axisLabel: { color: '#9aa4c4', fontSize: 9 },
        detail: { valueAnimation: true, formatter: `{value}${unit}`, color: '#e8ecf7', fontSize: 20 },
        data: [{ value }],
      },
    ],
  };
}
