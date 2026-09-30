// ChatBI 七步流水线的冻结顺序与展示名（对齐 §6.2 与《实现要点清单》）。

export const STEP_ORDER = [
  'intent',
  'schema_link',
  'sql_gen',
  'sql_guard',
  'sql_exec',
  'visual',
  'narrator',
] as const;

export type StepName = (typeof STEP_ORDER)[number];

export const STEP_LABELS: Record<string, string> = {
  intent: '意图理解',
  schema_link: 'Schema 链接',
  sql_gen: 'SQL 生成',
  sql_guard: 'SQL 校验',
  sql_exec: 'SQL 执行',
  visual: '可视化编排',
  narrator: '结论解读',
};

export function stepLabel(step: string): string {
  return STEP_LABELS[step] ?? step;
}
