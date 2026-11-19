// 大屏调色板（深色霓虹）。纯数据模块，不依赖 echarts，避免循环引用。

/** 各套调色板（按顺序取色，超出则循环）。 */
export const PALETTES: Record<string, string[]> = {
  neon: [
    '#22d3ee',
    '#4f8cff',
    '#a855f7',
    '#f472b6',
    '#facc15',
    '#34d399',
    '#fb7185',
    '#60a5fa',
  ],
  cyan: ['#06b6d4', '#0891b2', '#22d3ee', '#67e8f9', '#0ea5e9', '#38bdf8', '#7dd3fc', '#bae6fd'],
  aurora: [
    '#34d399',
    '#22d3ee',
    '#818cf8',
    '#c084fc',
    '#f472b6',
    '#fbbf24',
    '#4ade80',
    '#38bdf8',
  ],
};

/** 取调色板（未知名回退 neon）。 */
export function palette(name?: string | null): string[] {
  return PALETTES[name ?? 'neon'] ?? PALETTES.neon;
}

/** 主色（用于强调、描边、渐变起点）。 */
export function accentColor(name?: string | null): string {
  return palette(name)[0];
}
