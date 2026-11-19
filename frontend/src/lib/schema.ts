// 大屏 JSON 的运行时结构校验（§9.5 三条硬约定之一：schema_version 必填）。
//
// ★ 说明：设计文档举例用 zod；此处采用**零依赖的手写校验**，避免为最小集引入额外依赖。
//   校验目标与 zod 版一致：必填字段、`layout.cols === 12`、`panels[].kind` 合法、
//   `data_sources` 的 `mode` 合法。若后续需要更强的类型推导，可平滑替换为 zod。

import type { DashboardSpec, PanelKind } from '../types/dashboard';

export interface ValidationResult {
  ok: boolean;
  errors: string[];
}

const PANEL_KINDS: PanelKind[] = ['chart', 'metric_card', 'table', 'text', 'ranking'];

export function validateDashboardSpec(input: unknown): ValidationResult {
  const errors: string[] = [];
  const spec = input as Partial<DashboardSpec> | null | undefined;

  if (!spec || typeof spec !== 'object') {
    return { ok: false, errors: ['大屏 JSON 不是对象'] };
  }
  if (!spec.schema_version) errors.push('缺少 schema_version');
  if (!spec.dashboard_id) errors.push('缺少 dashboard_id');
  if (!spec.layout) {
    errors.push('缺少 layout');
  } else if (spec.layout.cols !== 12) {
    errors.push(`layout.cols 必须为 12，实得 ${String(spec.layout.cols)}`);
  }
  if (!Array.isArray(spec.panels)) {
    errors.push('panels 必须是数组');
  } else {
    spec.panels.forEach((panel, idx) => {
      if (!panel.panel_id) errors.push(`panels[${idx}] 缺少 panel_id`);
      if (!PANEL_KINDS.includes(panel.kind)) {
        errors.push(`panels[${idx}].kind 非法：${String(panel.kind)}`);
      }
    });
  }
  if (Array.isArray(spec.data_sources)) {
    spec.data_sources.forEach((ds, idx) => {
      // 用 unknown 承接：DataSource 联合类型本身只含 'inline'|'s3'，
      // 直接对 ds.mode 做 !== 判断会被 TS 收窄为 never。
      const mode: unknown = ds.mode;
      if (mode !== 'inline' && mode !== 's3') {
        errors.push(`data_sources[${idx}].mode 非法：${String(mode)}`);
      }
    });
  }

  return { ok: errors.length === 0, errors };
}
