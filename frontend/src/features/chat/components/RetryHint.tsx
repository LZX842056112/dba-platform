// 自愈提示：`sql.validation.failed` 到达时提示「SQL 有误，正在修正…」。

import type { RetryingInfo } from '../store/runStore';

interface Props {
  retrying: RetryingInfo | null;
}

export function RetryHint({ retrying }: Props) {
  if (!retrying) return null;
  return (
    <div className="hint hint--warn">
      SQL 有误，正在修正…（第 {retrying.attempt} 次）
      <div className="dim panel-card__sub">
        {retrying.code}
        {retrying.message ? ` · ${retrying.message}` : ''}
      </div>
    </div>
  );
}
