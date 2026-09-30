// 顶部提示条 + 错误区（预算告警 / 降级 / 拦截 / run.error / run.aborted）。

import type { RunError, RunStatus } from '../store/runStore';

interface Banner {
  kind: 'warn' | 'info' | 'error';
  text: string;
}

interface Props {
  banner: Banner | null;
  error: RunError | null;
  status: RunStatus;
}

export function RunBanner({ banner, error, status }: Props) {
  return (
    <>
      {banner ? <div className={`hint hint--${banner.kind}`}>{banner.text}</div> : null}
      {error ? (
        <div className="hint hint--error">
          <div>{error.message}</div>
          <div className="dim panel-card__sub">
            {error.code}
            {error.retryable ? ' · 可重试' : ''}
          </div>
        </div>
      ) : null}
      {status === 'done' ? <span className="badge badge--done">已完成</span> : null}
      {status === 'running' ? <span className="badge badge--running">进行中</span> : null}
    </>
  );
}
