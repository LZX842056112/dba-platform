// 文本面板（text）：直接渲染面板携带的静态文本。

import type { PanelSpec } from '../../../types/dashboard';

interface Props {
  panel: PanelSpec;
}

export function TextPanel({ panel }: Props) {
  return (
    <div style={{ flex: 1, minHeight: 0, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
      {panel.text ?? panel.subtitle ?? panel.title ?? '（无内容）'}
    </div>
  );
}
