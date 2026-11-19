// ECharts 深色主题定义（纯对象，不依赖 echarts，由 lib/echarts.ts 注册）。
//
// 为什么需要它：tokens.css 已是深色底（#0b1020），但 ECharts 默认是浅色主题 →
// 坐标轴/图例/提示文字是深色，在深底上几乎不可读。这里统一为深色。

import { palette } from './palettes';

export const DBA_DARK_THEME_NAME = 'dba-dark';

const TEXT = '#e8ecf7';
const DIM = '#9aa4c4';
const BORDER = '#2a3355';

/** 深色主题配置（`echarts.registerTheme(DBA_DARK_THEME_NAME, DBA_DARK_THEME)`）。 */
export const DBA_DARK_THEME = {
  color: palette('neon'),
  backgroundColor: 'transparent',
  textStyle: { color: TEXT },
  title: { textStyle: { color: TEXT }, subtextStyle: { color: DIM } },
  legend: { textStyle: { color: DIM } },
  tooltip: {
    backgroundColor: 'rgba(12,18,36,0.94)',
    borderColor: BORDER,
    borderWidth: 1,
    textStyle: { color: TEXT },
    axisPointer: { lineStyle: { color: BORDER }, crossStyle: { color: BORDER } },
  },
  categoryAxis: {
    axisLine: { lineStyle: { color: BORDER } },
    axisTick: { lineStyle: { color: BORDER } },
    axisLabel: { color: DIM },
    splitLine: { show: false, lineStyle: { color: 'rgba(42,51,85,0.45)' } },
  },
  valueAxis: {
    axisLine: { show: false, lineStyle: { color: BORDER } },
    axisTick: { lineStyle: { color: BORDER } },
    axisLabel: { color: DIM },
    splitLine: { lineStyle: { color: 'rgba(42,51,85,0.45)' } },
  },
  visualMap: { textStyle: { color: DIM } },
  geo: {
    itemStyle: { areaColor: '#141a2e', borderColor: '#31406e' },
    emphasis: { itemStyle: { areaColor: '#1f2c52' }, label: { color: TEXT } },
    label: { color: DIM },
  },
};
