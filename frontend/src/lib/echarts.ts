// ECharts 按需注册（tree-shaking，§9.6）。
// 完整包约 1MB，按需注册后主包可压到 300KB 以内。

import * as echarts from 'echarts/core';
import {
  BarChart,
  EffectScatterChart,
  GaugeChart,
  GraphChart,
  LineChart,
  MapChart,
  PieChart,
  ScatterChart,
} from 'echarts/charts';
import {
  DatasetComponent,
  GeoComponent,
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';

import { DBA_DARK_THEME, DBA_DARK_THEME_NAME } from './echartsTheme';

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  ScatterChart,
  GaugeChart,
  MapChart, // ★ 地图（choropleth）
  EffectScatterChart, // ★ 地图涟漪散点
  GraphChart, // ★ 拓扑关系图
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DatasetComponent,
  TitleComponent,
  GeoComponent, // ★ 地理坐标系
  VisualMapComponent, // ★ 地图分级设色
  MarkLineComponent, // ★ 仪表盘目标线
  GraphicComponent,
  CanvasRenderer,
]);

// 注册深色主题（地图对象见 lib/echartsTheme.ts，避免与本模块循环引用）
echarts.registerTheme(DBA_DARK_THEME_NAME, DBA_DARK_THEME);

export { echarts, DBA_DARK_THEME_NAME };
export type { EChartsCoreOption };
