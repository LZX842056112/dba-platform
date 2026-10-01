// 大屏 JSON 布局规范的 TS 类型（对齐《设计方案 v2》§9.5）。
// schema_version 必填：前端按版本做兼容分支。

export type ChartType = 'line' | 'bar' | 'pie' | 'scatter' | 'map' | 'gauge';
export type PanelKind = 'chart' | 'metric_card' | 'table' | 'text' | 'ranking';
export type FieldType = 'time' | 'number' | 'string' | 'boolean';
export type Aggregate = 'sum' | 'avg' | 'min' | 'max' | 'count' | 'none';

export interface EncodingField {
  field: string;
  type?: FieldType;
  agg?: Aggregate;
}

export interface Encoding {
  x?: EncodingField;
  y?: EncodingField[];
  series?: EncodingField;
  color?: EncodingField | null;
  lon?: EncodingField | null; // 地图散点经度
  lat?: EncodingField | null; // 地图散点纬度
  size?: EncodingField | null; // 气泡大小
}

export interface ChartSpec {
  type: ChartType;
  region?: string; // map 用：已注册的地图名，默认 'china'
}

/** 面板样式（与后端 `PanelStyle` 对齐；后端 extra=allow，故带索引签名）。 */
export interface PanelStyle {
  // 通用
  palette?: string; // neon | cyan | aurora
  legend?: 'top' | 'bottom' | 'none';
  unit?: string;
  precision?: number;
  accent?: string;
  grid?: boolean;
  // 序列类
  stack?: boolean;
  area?: boolean;
  smooth?: boolean;
  sort?: 'none' | 'asc' | 'desc';
  topN?: number;
  showBackground?: boolean;
  orientation?: 'vertical' | 'horizontal';
  // 饼 / 环 / 玫瑰
  pieVariant?: 'pie' | 'donut' | 'rose' | 'ring';
  innerRadius?: number;
  // 仪表盘
  gaugeMax?: number;
  gaugeTarget?: number;
  // 地图
  mapScatter?: boolean;
  mapZoom?: number;
  // KPI 卡
  variant?: 'plain' | 'flip' | 'neon';
  trend?: 'up' | 'down' | 'flat';
  trendValue?: number;
  animate?: boolean;
  compare?: string;
  [key: string]: unknown;
}

export interface LayoutItem {
  i: string; // panel_id
  x: number;
  y: number;
  w: number; // 12 列栅格宽度
  h: number;
  minW?: number;
  minH?: number;
}

export interface Layout {
  type: 'grid';
  cols: 12;
  rowHeight: number;
  gap: [number, number];
  items: LayoutItem[];
}

export interface DataSourceInline {
  ref: string;
  mode: 'inline';
  columns: Array<{ name: string; type: string }>;
  rows: Array<Array<string | number | boolean | null>>;
}

export interface DataSourceS3 {
  ref: string;
  mode: 's3';
  object_key: string;
  presigned_url: string;
  expires_at: string;
  row_count: number;
  format: string;
}

export type DataSource = DataSourceInline | DataSourceS3;

export interface PanelSpec {
  panel_id: string;
  kind: PanelKind;
  title?: string;
  subtitle?: string;
  chart?: ChartSpec;
  encoding?: Encoding;
  dataset?: { ref: string };
  query?: {
    metric_codes?: string[];
    biz_line_id?: number;
    scope_hash?: string;
  };
  interaction?: { linked?: boolean; drilldown?: string };
  style?: PanelStyle;
  text?: string;
}

export interface DashboardSpec {
  schema_version: string;
  dashboard_id: string;
  version: number;
  title?: string;
  theme?: 'light' | 'dark';
  layout: Layout;
  panels: PanelSpec[];
  data_sources: DataSource[];
  meta?: {
    generated_by_trace?: string;
    generated_at?: string;
    generator?: string;
  };
}

/** 行数据集（把 data_source 展开为「列名 → 值」的对象数组，便于渲染）。 */
export type RowRecord = Record<string, string | number | boolean | null>;
