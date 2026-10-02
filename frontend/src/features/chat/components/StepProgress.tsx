// 七步流水线进度条（消费 agent.step.* 事件）。

import { STEP_ORDER, stepLabel } from '../constants';
import type { StepState } from '../store/runStore';
import { formatMs } from '../../../lib/format';

interface Props {
  steps: Record<string, StepState>;
}

function badgeClass(status: StepState['status'] | 'idle'): string {
  switch (status) {
    case 'running':
      return 'badge badge--running';
    case 'retrying':
      return 'badge badge--warn';
    case 'done':
      return 'badge badge--done';
    case 'error':
      return 'badge badge--error';
    default:
      return 'badge';
  }
}

function badgeText(status: StepState['status'] | 'idle'): string {
  switch (status) {
    case 'running':
      return '进行中';
    case 'retrying':
      return '重试中';
    case 'done':
      return '完成';
    case 'error':
      return '失败';
    default:
      return '待处理';
  }
}

export function StepProgress({ steps }: Props) {
  return (
    <ol className="step-list">
      {STEP_ORDER.map((step) => {
        const state = steps[step];
        const status = state?.status ?? 'idle';
        return (
          <li key={step}>
            <span className={badgeClass(status)}>{badgeText(status)}</span>
            <span>{stepLabel(step)}</span>
            {status === 'done' && state?.durationMs !== undefined ? (
              <span className="dim panel-card__sub">{formatMs(state.durationMs)}</span>
            ) : null}
            {status === 'retrying' && state ? (
              <span className="dim panel-card__sub">第 {state.attempt} 次</span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
