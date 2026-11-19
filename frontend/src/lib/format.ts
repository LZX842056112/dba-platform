// 展示格式化：微美元 → 人类可读；时间格式化（§9.1 lib/format）。

const MICRO = 1_000_000;

/** 微美元 → 展示字符串（保留到分，必要时按需扩展）。 */
export function formatMicroUsd(micro: number | null | undefined): string {
  if (micro === null || micro === undefined) return '—';
  const usd = micro / MICRO;
  if (Math.abs(usd) >= 1) return `$${usd.toFixed(2)}`;
  if (usd === 0) return '$0.00';
  return `$${usd.toFixed(4)}`;
}

/** 毫秒 → 展示（<1000ms 用 ms，否则用 s）。 */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** 数值 → 千分位展示。 */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return new Intl.NumberFormat('zh-CN').format(value);
}

/** ISO / 毫秒时间戳 → 本地时间展示。 */
export function formatTime(input: string | number | null | undefined): string {
  if (input === null || input === undefined) return '—';
  const date = typeof input === 'number' ? new Date(input) : new Date(input);
  if (Number.isNaN(date.getTime())) return String(input);
  return date.toLocaleString('zh-CN');
}
