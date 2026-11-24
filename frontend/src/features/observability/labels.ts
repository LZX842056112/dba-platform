// 观测模块展示词表：后端枚举值 → 中文标签。
//
// 为什么集中在这里：Run 状态、模块名、span 类型在「流程监控 / 运行明细 / 运行详情」
// 三处都要显示。分散写会出现「成功」与「success」两种写法，列宽与排序都会漂移。
// 只放词表与查表函数，不放组件（避免把 React 依赖带进数据层）。

/** 取不到值时的统一占位符（全站「诚实空态」约定）。 */
export const PLACEHOLDER = '—';

/** 霓虹色调用途（流程监控的角标用 data-tone）。 */
export type RunTone = 'run' | 'ok' | 'err' | 'warn';

export interface RunStatusMeta {
  /** 中文标签 */
  label: string;
  /** 色调（流程监控的行高亮 / 角标） */
  tone: RunTone;
  /** global.css 中已有的 badge 修饰类（运行明细、运行详情用） */
  badgeClass: string;
}

/** run.status 枚举（storage/mysql/models.py Run.status）。 */
export const RUN_STATUS: Record<string, RunStatusMeta> = {
  running: { label: '运行中', tone: 'run', badgeClass: 'badge--running' },
  success: { label: '成功', tone: 'ok', badgeClass: 'badge--done' },
  failed: { label: '失败', tone: 'err', badgeClass: 'badge--error' },
  timeout: { label: '超时', tone: 'warn', badgeClass: 'badge--warn' },
  aborted: { label: '中止', tone: 'warn', badgeClass: 'badge--warn' },
};

/** 未知状态原样显示，不猜语义。 */
export function runStatusMeta(status: unknown): RunStatusMeta {
  const key = String(status ?? '');
  return RUN_STATUS[key] ?? { label: key || '未知', tone: 'warn', badgeClass: 'badge--warn' };
}

/** 视为「失败」的状态集合（统计失败数、染色用）。 */
export const FAILED_RUN_STATUS: ReadonlySet<string> = new Set(['failed', 'timeout', 'aborted']);

/** run.module 枚举（storage/mysql/models.py Run.module）。 */
export const MODULES: Record<string, { label: string; short: string }> = {
  chatbi: { label: 'ChatBI 问答', short: 'ChatBI' },
  observability: { label: '观测模块', short: '观测' },
  finops: { label: 'FinOps', short: 'FinOps' },
  system: { label: '系统任务', short: '系统' },
};

/** @param short 表格等窄场景用短名（观测 / 系统），卡片用全名。 */
export function moduleLabel(module: unknown, short = false): string {
  const key = String(module ?? '');
  const hit = MODULES[key];
  if (hit) return short ? hit.short : hit.label;
  return key || PLACEHOLDER;
}

/** span.status 枚举（agent-runtime telemetry 埋点）。 */
export const SPAN_STATUS: Record<string, { label: string; badgeClass: string }> = {
  ok: { label: '成功', badgeClass: 'badge--done' },
  error: { label: '失败', badgeClass: 'badge--error' },
  timeout: { label: '超时', badgeClass: 'badge--warn' },
};

export function spanStatusMeta(status: unknown): { label: string; badgeClass: string } {
  const key = String(status ?? '');
  return SPAN_STATUS[key] ?? { label: key || '未知', badgeClass: 'badge--warn' };
}

/** span.kind 枚举（拓扑只收 agent / tool / sub_agent，llm 为细粒度调用）。 */
export const SPAN_KIND: Record<string, string> = {
  agent: 'Agent',
  tool: '工具',
  sub_agent: '子 Agent',
  llm: '模型调用',
  run: '整体运行',
};

export function spanKindLabel(kind: unknown): string {
  const key = String(kind ?? '');
  return SPAN_KIND[key] ?? (key || PLACEHOLDER);
}

/** alert_event.severity 枚举（MySQL alert_severity）。 */
export const ALERT_SEVERITY: Record<string, { label: string; badgeClass: string }> = {
  info: { label: '提示', badgeClass: 'badge--info' },
  warn: { label: '警告', badgeClass: 'badge--warn' },
  critical: { label: '严重', badgeClass: 'badge--error' },
};

export function alertSeverityMeta(severity: unknown): { label: string; badgeClass: string } {
  const key = String(severity ?? '');
  return ALERT_SEVERITY[key] ?? { label: key || '未知', badgeClass: 'badge--warn' };
}

/** alert_event.category 枚举（MySQL alert_category）。 */
export const ALERT_CATEGORY: Record<string, string> = {
  cost_spike: '成本突增',
  fail_rate_up: '失败率上升',
  skill_rot: '技能废弃',
  silent_failure: '静默失败',
  loop_suspect: '疑似死循环',
};

export function alertCategoryLabel(category: unknown): string {
  const key = String(category ?? '');
  return ALERT_CATEGORY[key] ?? (key || PLACEHOLDER);
}

/** alert_event.status 枚举（MySQL alert_status）。 */
export const ALERT_STATUS: Record<string, { label: string; badgeClass: string }> = {
  open: { label: '待处理', badgeClass: 'badge--warn' },
  acked: { label: '已确认', badgeClass: 'badge--info' },
  resolved: { label: '已解决', badgeClass: 'badge--done' },
  ignored: { label: '已忽略', badgeClass: '' },
};

export function alertStatusMeta(status: unknown): { label: string; badgeClass: string } {
  const key = String(status ?? '');
  return ALERT_STATUS[key] ?? { label: key || '未知', badgeClass: 'badge--warn' };
}
