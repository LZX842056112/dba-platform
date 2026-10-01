// 驾驶舱外壳：头部标题栏 + 背景网格 + 装饰描边（纯 CSS，零重依赖）。
//
// ★ 注意：外层 `.panel-title`（「数据大屏」）由 ChatSessionPage 保留，
//   浏览器 e2e 断言 `.panel-title` ≥ 2 依赖它，勿移除。

import { useEffect, useState, type ReactNode } from 'react';

import '../../../styles/cockpit.css';

interface Props {
  children: ReactNode;
  /** 大屏标题（取自 spec.title）。 */
  title?: string;
  /** 面板数量（展示在右上角）。 */
  count?: number;
}

function useClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now.toLocaleTimeString('zh-CN', { hour12: false });
}

export function CockpitFrame({ children, title, count }: Props) {
  const clock = useClock();

  return (
    <div className="cockpit">
      <div className="cockpit__bg" aria-hidden />
      <header className="cockpit__head">
        <span className="cockpit__title">{title || '数据驾驶舱'}</span>
        <span className="cockpit__meta">
          {typeof count === 'number' ? `${count} 个面板` : ''}
          {typeof count === 'number' ? ' · ' : ''}
          {clock}
        </span>
      </header>
      <div className="cockpit__body">{children}</div>
    </div>
  );
}
