// ECharts 基座封装（★ 对齐《设计方案 v2》§9.6，含 P1-10 修复）。
//
// 大屏面板数量多，必须有统一基座，否则每个面板都写一遍 init / setOption / resize / dispose
// 会造成大量重复代码与内存泄漏。
//
// ★ v2 修复（v1 的两个 bug）：
//   ① v1 的清理写死 `off('click')`：同时绑 click / legendselect / datazoom 时，
//      只有 click 被解绑，其余 handler 泄漏；
//   ② onEvents 每次变化都重复注册同一批 handler → 一次点击触发 N 次回调。
//   正确做法：记录本次绑定的 **(event, fn) 对**，按对精确解绑。

import { useEffect, useRef } from 'react';
import { echarts, type EChartsCoreOption } from '../../lib/echarts';

type EChartsInstance = ReturnType<typeof echarts.init>;
export type EChartsEventHandler = (params: unknown) => void;

interface Props {
  option: EChartsCoreOption;
  /** 容器高度（number → px；string → 原样，如 '100%'）。 */
  height?: number | string;
  onEvents?: Record<string, EChartsEventHandler>;
}

export function EchartsBase({ option, height = '100%', onEvents }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<EChartsInstance | null>(null);

  // 初始化 / 销毁：只跑一次
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    instanceRef.current = echarts.init(container, undefined, { renderer: 'canvas' });
    const observer = new ResizeObserver(() => instanceRef.current?.resize());
    observer.observe(container);
    return () => {
      observer.disconnect();
      instanceRef.current?.dispose();
      instanceRef.current = null;
    };
  }, []);

  // ★ notMerge:false —— 增量更新，避免流式出图时闪烁
  useEffect(() => {
    instanceRef.current?.setOption(option, { notMerge: false, lazyUpdate: true });
  }, [option]);

  // ★ 事件绑定：按 (event, fn) 对精确解绑（P1-10）
  useEffect(() => {
    const instance = instanceRef.current;
    if (!onEvents || !instance) return;
    const bound = Object.entries(onEvents) as Array<[string, EChartsEventHandler]>;
    bound.forEach(([event, handler]) => instance.on(event, handler));
    return () => {
      const current = instanceRef.current;
      if (!current) return;
      bound.forEach(([event, handler]) => current.off(event, handler));
    };
  }, [onEvents]);

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height: typeof height === 'number' ? `${height}px` : height }}
    />
  );
}
