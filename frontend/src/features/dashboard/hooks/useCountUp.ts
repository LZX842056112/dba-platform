// 数字翻牌动画：从 0 递增到目标值（ease-out cubic）。
// 用 requestAnimationFrame，不引入动画库。

import { useEffect, useRef, useState } from 'react';

/**
 * @param target    目标值
 * @param durationMs 动画时长
 * @param enabled   关闭时直接返回目标值（尊重 prefers-reduced-motion 由调用方决定）
 */
export function useCountUp(target: number, durationMs = 900, enabled = true): number {
  const [value, setValue] = useState(enabled ? 0 : target);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled || !Number.isFinite(target)) {
      setValue(target);
      return;
    }
    const start = performance.now();
    const tick = (now: number): void => {
      const progress = Math.min(1, (now - start) / durationMs);
      const eased = 1 - (1 - progress) ** 3;
      setValue(target * eased);
      if (progress < 1) frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, [target, durationMs, enabled]);

  return value;
}
