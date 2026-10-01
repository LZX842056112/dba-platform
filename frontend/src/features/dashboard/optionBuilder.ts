// 面板 → ECharts option 的纯函数装配（§9.5 encoding 约定）。
// 保持零 React 依赖，便于单测与复用。
//
// ★ 深色基底：主题（lib/echartsTheme.ts）负责全局观感，本文件只处理**逐面板差异**
//   （调色板、渐变、逐系列配色），两者职责分离。

import type { EChartsCoreOption } from '../../lib/echarts';
import { accentColor, palette } from '../../lib/palettes';
import type { Aggregate, PanelSpec, PanelStyle, RowRecord } from '../../types/dashboard';

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

// ── 地图名称对齐（GeoJSON 里是省级全称，如「广东省」「北京市」）──────
const PROVINCE_ALIASES: Record<string, string> = {
  北京: '北京市',
  天津: '天津市',
  上海: '上海市',
  重庆: '重庆市',
  河北: '河北省',
  山西: '山西省',
  辽宁: '辽宁省',
  吉林: '吉林省',
  黑龙江: '黑龙江省',
  江苏: '江苏省',
  浙江: '浙江省',
  安徽: '安徽省',
  福建: '福建省',
  江西: '江西省',
  山东: '山东省',
  河南: '河南省',
  湖北: '湖北省',
  湖南: '湖南省',
  广东: '广东省',
  海南: '海南省',
  四川: '四川省',
  贵州: '贵州省',
  云南: '云南省',
  陕西: '陕西省',
  甘肃: '甘肃省',
  青海: '青海省',
  台湾: '台湾省',
  内蒙古: '内蒙古自治区',
  广西: '广西壮族自治区',
  西藏: '西藏自治区',
  宁夏: '宁夏回族自治区',
  新疆: '新疆维吾尔自治区',
  香港: '香港特别行政区',
  澳门: '澳门特别行政区',
};

/** 省份名归一化到 GeoJSON 的 `properties.name`（未命中时告警并原样返回）。 */
export function normalizeProvince(name: string): string {
  if (name in PROVINCE_ALIASES) return PROVINCE_ALIASES[name];
  if (name.endsWith('省') || name.endsWith('市') || name.endsWith('区')) return name;
  const hit = Object.entries(PROVINCE_ALIASES).find(([short]) => name.startsWith(short));
  if (hit) return hit[1];
  console.warn('[dba] 地图省份名未匹配到 GeoJSON：', name);
  return name;
}

// ── 通用基底 ────────────────────────────────────────────────────────
function legendOf(style: PanelStyle) {
  const legend = style.legend ?? 'top';
  return legend === 'none' ? undefined : { top: legend, textStyle: { color: '#9aa4c4' } };
}

/** 逐面板深色基底（背景透明 + 调色板）。 */
function baseOption(style: PanelStyle): EChartsCoreOption {
  return { backgroundColor: 'transparent', color: palette(style.palette) };
}

interface SeriesDatum {
  name: string;
  type: string;
  data: unknown;
  smooth?: boolean;
  stack?: string;
  areaStyle?: unknown;
  itemStyle?: unknown;
  label?: unknown;
  radius?: string | string[];
  roseType?: string;
  center?: string[];
  showBackground?: boolean;
  backgroundStyle?: unknown;
  emphasis?: unknown;
  rippleEffect?: unknown;
  symbolSize?: number;
  coordinateSystem?: string;
  geoIndex?: number;
  tooltip?: unknown;
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
  const style: PanelStyle = panel.style ?? {};
  const xField = encoding.x?.field;
  const yMeta = encoding.y ?? [];
  const seriesField = encoding.series?.field;
  const stack = style.stack ? 'total' : undefined;

  // ── 地图（choropleth + 可选涟漪散点）────────────────────────────
  if (type === 'map') {
    const nameField = xField ?? yMeta[0]?.field ?? '';
    const valueField = (yMeta[0]?.field ?? xField ?? '') as string;
    const data = rows.map((row) => ({
      name: normalizeProvince(String(row[nameField] ?? '')),
      value: numeric(row[valueField]) ?? 0,
    }));
    const values = data.map((d) => d.value).filter((v) => Number.isFinite(v));
    const region = panel.chart?.region ?? 'china';
    const series: SeriesDatum[] = [
      {
        name: valueField,
        type: 'map',
        // ★ 必须绑定上面的 geo 坐标系：仅写 `geo` 而系列不给 `geoIndex/map` 时，
        //   ECharts 的 MapSeries 找不到地图数据，会抛
        //   "Cannot read properties of undefined (reading 'regions')" 并整页白屏。
        geoIndex: 0,
        data,
        emphasis: {
          label: { show: true, color: '#e8ecf7' },
          itemStyle: { areaColor: '#1f2c52' },
        },
        itemStyle: { borderColor: '#31406e', borderWidth: 0.6 },
        label: { show: false },
      },
    ];

    // 涟漪散点：需要 lon/lat 两个字段，缺任一个就只画分级设色图
    const lonField = encoding.lon?.field;
    const latField = encoding.lat?.field;
    if (style.mapScatter && lonField && latField) {
      const points = rows
        .map((row) => {
          const lon = numeric(row[lonField]);
          const lat = numeric(row[latField]);
          if (lon === null || lat === null) return null;
          return {
            name: String(row[nameField] ?? ''),
            value: [lon, lat, numeric(row[valueField]) ?? 0],
          };
        })
        .filter((p): p is { name: string; value: number[] } => p !== null);
      if (points.length > 0) {
        series.push({
          name: '分布',
          type: 'effectScatter',
          coordinateSystem: 'geo',
          data: points,
          symbolSize: 8,
          rippleEffect: { brushType: 'stroke', scale: 3 },
          itemStyle: { color: accentColor(style.palette) },
          label: { show: false },
        });
      }
    }

    return {
      ...baseOption(style),
      tooltip: { trigger: 'item' },
      geo: {
        map: region,
        roam: true,
        // ★ 合规：默认不放大（zoom=1），确保含南海诸岛/九段线的完整疆域不被裁出可视区；
        //   如需放大，必须自行确认九段线仍在视野内。
        zoom: style.mapZoom ?? 1,
        itemStyle: { areaColor: '#141a2e', borderColor: '#31406e' },
        emphasis: { itemStyle: { areaColor: '#1f2c52' }, label: { color: '#e8ecf7' } },
        label: { show: false },
      },
      visualMap: {
        type: 'continuous',
        min: values.length ? Math.min(...values) : 0,
        max: values.length ? Math.max(...values) : 1,
        inRange: { color: ['#16223f', '#2563eb', '#22d3ee'] },
        textStyle: { color: '#9aa4c4' },
        left: 12,
        bottom: 12,
        calculable: true,
      },
      series,
    };
  }

  // ── 仪表盘 ──────────────────────────────────────────────────────
  if (type === 'gauge') {
    const value = buildMetricValue(panel, rows);
    const max = style.gaugeMax ?? Math.max(100, Math.ceil(value * 1.2));
    return {
      ...baseOption(style),
      series: [
        {
          type: 'gauge',
          min: 0,
          max,
          startAngle: 210,
          endAngle: -30,
          progress: { show: true, width: 12, roundCap: true },
          axisLine: { lineStyle: { width: 12, color: [[1, 'rgba(42,51,85,0.6)']] } },
          pointer: { itemStyle: { color: accentColor(style.palette) } },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: '#2a3355' } },
          axisLabel: { color: '#9aa4c4', fontSize: 10 },
          detail: {
            valueAnimation: true,
            formatter: '{value}',
            color: '#e8ecf7',
            fontSize: 26,
            offsetCenter: [0, '62%'],
          },
          data: [{ value, name: panel.encoding?.y?.[0]?.field ?? '' }],
          title: { color: '#9aa4c4', fontSize: 12 },
          markLine: style.gaugeTarget
            ? {
                data: [{ yAxis: style.gaugeTarget }],
                lineStyle: { color: '#f472b6', type: 'dashed' },
              }
            : undefined,
        },
      ],
    };
  }

  // ── 饼 / 环 / 玫瑰 ──────────────────────────────────────────────
  if (type === 'pie') {
    const nameField = xField ?? yMeta[0]?.field ?? '';
    const valueField = yMeta[0]?.field ?? xField ?? '';
    const raw = rows.map((row) => ({
      name: String(row[nameField] ?? ''),
      value: numeric(row[valueField]) ?? 0,
    }));
    const sorted = [...raw].sort((a, b) =>
      style.sort === 'asc' ? a.value - b.value : b.value - a.value,
    );
    const pieData = style.topN && style.topN > 0 ? sorted.slice(0, style.topN) : sorted;
    const variant = style.pieVariant ?? 'pie';
    const radius =
      style.innerRadius !== undefined
        ? [`${style.innerRadius}%`, '76%']
        : variant === 'donut'
          ? ['52%', '74%']
          : variant === 'ring'
            ? ['70%', '80%']
            : '62%';
    return {
      ...baseOption(style),
      tooltip: { trigger: 'item' },
      legend: legendOf(style),
      series: [
        {
          type: 'pie',
          radius,
          roseType: variant === 'rose' ? 'radius' : undefined,
          center: ['50%', '50%'],
          itemStyle: { borderColor: '#0b1020', borderWidth: 2, borderRadius: 4 },
          label: { color: '#9aa4c4', formatter: '{b} {d}%' },
          labelLine: { lineStyle: { color: '#2a3355' } },
          emphasis: { scale: true, itemStyle: { shadowBlur: 12, shadowColor: 'rgba(34,211,238,0.5)' } },
          data: pieData,
        },
      ],
    };
  }

  // ── 无 x 轴（退化）：按行序号出单序列 ────────────────────────────
  if (!xField) {
    const series: SeriesDatum[] = yMeta.map((meta) => ({
      name: meta.field,
      type,
      data: rows.map((row) => numeric(row[meta.field]) ?? 0),
      smooth: style.smooth ?? type === 'line',
      stack,
      areaStyle: style.area && type === 'line' ? { opacity: 0.25 } : undefined,
    }));
    return {
      ...baseOption(style),
      tooltip: { trigger: 'axis' },
      legend: legendOf(style),
      xAxis: { type: 'category', data: rows.map((_, index) => String(index + 1)) },
      yAxis: { type: 'value' },
      series,
    };
  }

  // ── 横向（排行榜）──────────────────────────────────────────────
  if (style.orientation === 'horizontal') {
    const valueField = yMeta[0]?.field ?? '';
    const items = rows
      .map((row) => ({
        name: String(row[xField] ?? ''),
        value: numeric(row[valueField]) ?? 0,
      }))
      .sort((a, b) => (style.sort === 'asc' ? a.value - b.value : b.value - a.value));
    const trimmed = style.topN && style.topN > 0 ? items.slice(0, style.topN) : items;
    // ECharts 横向柱：类目轴在 y，数值轴在 x；从下往上画，故反转
    return {
      ...baseOption(style),
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 8, right: 24, top: 12, bottom: 8, containLabel: true },
      xAxis: { type: 'value' },
      yAxis: { type: 'category', data: trimmed.map((d) => d.name).reverse() },
      series: [
        {
          name: valueField,
          type: 'bar',
          data: trimmed.map((d) => d.value).reverse(),
          showBackground: Boolean(style.showBackground),
          backgroundStyle: { color: 'rgba(42,51,85,0.35)', borderRadius: 6 },
          itemStyle: {
            borderRadius: 6,
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 1,
              y2: 0,
              colorStops: [
                { offset: 0, color: 'rgba(37,99,235,0.75)' },
                { offset: 1, color: accentColor(style.palette) },
              ],
            },
          },
          label: { show: true, position: 'right', color: '#e8ecf7', fontSize: 11 },
        },
      ],
    };
  }

  // ── 常规类目轴（line / bar / scatter）───────────────────────────
  const categories = Array.from(new Set(rows.map((row) => String(row[xField] ?? ''))));
  const series: SeriesDatum[] = [];
  const useStack = type === 'bar' || type === 'line' ? stack : undefined;

  if (seriesField) {
    const groups = Array.from(new Set(rows.map((row) => String(row[seriesField] ?? ''))));
    for (const group of groups) {
      const subset = rows.filter((row) => String(row[seriesField] ?? '') === group);
      for (const meta of yMeta) {
        series.push({
          name: `${meta.field}·${group}`,
          type,
          data: groupByCategory(subset, categories, xField, meta.field, meta.agg),
          smooth: style.smooth ?? type === 'line',
          stack: useStack,
          areaStyle: style.area && type === 'line' ? { opacity: 0.25 } : undefined,
        });
      }
    }
  } else {
    for (const meta of yMeta) {
      series.push({
        name: meta.field,
        type,
        data: groupByCategory(rows, categories, xField, meta.field, meta.agg),
        smooth: style.smooth ?? type === 'line',
        stack: useStack,
        areaStyle: style.area && type === 'line' ? { opacity: 0.25 } : undefined,
      });
    }
  }

  return {
    ...baseOption(style),
    tooltip: { trigger: 'axis' },
    legend: legendOf(style),
    grid: { left: 8, right: 16, top: legendOf(style) ? 36 : 16, bottom: 8, containLabel: true },
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
