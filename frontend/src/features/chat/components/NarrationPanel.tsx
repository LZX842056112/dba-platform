// 结论解读（打字机效果）+ 成本/用量展示。

import { formatMicroUsd } from '../../../lib/format';
import type { UsageData } from '../../../types/events';

interface Props {
  text: string;
  usage: UsageData | null;
}

export function NarrationPanel({ text, usage }: Props) {
  if (!text && !usage) return null;
  return (
    <div className="bubble">
      {text ? <div>{text}</div> : <div className="dim">（正在生成结论…）</div>}
      {usage ? (
        <div className="dim panel-card__sub" style={{ marginTop: 6 }}>
          tokens：{usage.prompt_tokens} + {usage.completion_tokens} · 成本 {formatMicroUsd(usage.cost_micro_usd)}
        </div>
      ) : null}
    </div>
  );
}
