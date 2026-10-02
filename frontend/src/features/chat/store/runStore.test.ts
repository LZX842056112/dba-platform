import { beforeEach, describe, expect, it } from 'vitest';
import { useRunStore } from './runStore';
import type { EventEnvelope, EventType } from '../../../types/events';

function event(eventName: EventType, seq: number, data: Record<string, unknown>): EventEnvelope {
  return { event: eventName, seq, trace_id: 'trace', ts: 0, data };
}

describe('runStore terminal events', () => {
  beforeEach(() => useRunStore.getState().reset());

  it('marks pending steps failed and clears retry feedback for unsuccessful runs', () => {
    const { apply } = useRunStore.getState();
    apply(event('run.started', 1, {}));
    apply(event('agent.step.retrying', 2, { step: 'sql_gen', attempt: 2 }));
    apply(event('sql.validation.failed', 3, { attempt: 1, code: 'BAD_SQL', message: '校验失败' }));
    apply(event('run.finished', 4, { status: 'timeout' }));

    const state = useRunStore.getState();
    expect(state.status).toBe('error');
    expect(state.retrying).toBeNull();
    expect(state.steps.sql_gen?.status).toBe('error');
    expect(state.error?.message).toContain('timeout');
  });

  it('completes pending steps and clears retry feedback for successful runs', () => {
    const { apply } = useRunStore.getState();
    apply(event('run.started', 1, {}));
    apply(event('agent.step.started', 2, { step: 'sql_gen', attempt: 1 }));
    apply(event('sql.validation.failed', 3, { attempt: 1, code: 'BAD_SQL', message: '校验失败' }));
    apply(event('run.finished', 4, { status: 'success' }));

    const state = useRunStore.getState();
    expect(state.status).toBe('done');
    expect(state.retrying).toBeNull();
    expect(state.steps.sql_gen?.status).toBe('done');
    expect(state.error).toBeNull();
  });

  it('marks an SSE gap and records snapshot recovery without changing the run cursor', () => {
    const store = useRunStore.getState();
    store.apply(event('run.started', 1, {}));
    store.markReplayGap({ afterSeq: 1, oldestAvailableSeq: 1048, latestSeq: 1247 });
    expect(useRunStore.getState().replayGap?.recovered).toBe(false);
    expect(useRunStore.getState().lastSeq).toBe(1);

    useRunStore.getState().recoverReplayGap(3, 'Recovered answer');
    const state = useRunStore.getState();
    expect(state.replayGap?.recovered).toBe(true);
    expect(state.narration).toBe('Recovered answer');
    expect(state.lastSeq).toBe(1);
    expect(state.banner?.text).toContain('3 个 span');
  });
});
