import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RunStreamHandlers } from '../../../api/sse';
import { QueryBar } from '../components/QueryBar';
import { useRunStore } from '../store/runStore';
import { useChatRun } from './useChatRun';

const mocks = vi.hoisted(() => ({
  handlers: undefined as RunStreamHandlers | undefined,
  createSession: vi.fn(),
  startQuery: vi.fn(),
  getRunDoc: vi.fn(),
  listSessionMessages: vi.fn(),
}));

vi.mock('../../../api/sse', () => ({
  openRunStream: (_traceId: string, _ticket: string, handlers: RunStreamHandlers) => {
    mocks.handlers = handlers;
    return vi.fn();
  },
}));

vi.mock('../api/chatApi', () => ({
  createSession: mocks.createSession,
  startQuery: mocks.startQuery,
  getRunDoc: mocks.getRunDoc,
  listSessionMessages: mocks.listSessionMessages,
}));

function QueryHarness() {
  const { start, busy } = useChatRun();
  return <QueryBar onSubmit={start} disabled={busy} />;
}

function renderHarness() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <QueryHarness />
    </QueryClientProvider>,
  );
}

describe('useChatRun input lifecycle', () => {
  beforeEach(() => {
    cleanup();
    mocks.handlers = undefined;
    useRunStore.getState().reset();
    mocks.createSession.mockResolvedValue({ session_id: 'session' });
    mocks.startQuery.mockResolvedValue({ trace_id: 'trace', stream_ticket: 'ticket' });
    mocks.getRunDoc.mockResolvedValue({ spans: [{ span_id: 'span-1' }] });
    mocks.listSessionMessages.mockResolvedValue([
      { role: 'assistant', content: 'Recovered answer', trace_id: 'trace' },
    ]);
  });

  it('keeps the query input disabled during streaming and restores it on terminal event', async () => {
    let resolveQuery: ((result: { trace_id: string; stream_ticket: string }) => void) | undefined;
    mocks.startQuery.mockReturnValue(
      new Promise((resolve) => {
        resolveQuery = resolve;
      }),
    );
    renderHarness();
    const input = screen.getByPlaceholderText('例如：华东上个月 GMV 多少？') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '查询 GMV' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);

    await waitFor(() => expect(mocks.startQuery).toHaveBeenCalled());
    expect(input.disabled).toBe(true);
    await act(async () => {
      resolveQuery?.({ trace_id: 'trace', stream_ticket: 'ticket' });
    });
    await waitFor(() => expect(mocks.handlers).toBeDefined());
    expect(input.disabled).toBe(true);

    await act(async () => {
      mocks.handlers?.onEvent({
        event: 'run.finished',
        seq: 1,
        trace_id: 'trace',
        ts: 0,
        data: { status: 'success' },
      });
    });
    await waitFor(() => expect(input.disabled).toBe(false));
  });

  it('ends a stuck local run when the fallback snapshot cannot provide a terminal state', async () => {
    renderHarness();
    const input = screen.getByPlaceholderText('例如：华东上个月 GMV 多少？') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '查询 GMV' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(mocks.handlers).toBeDefined());

    await act(async () => {
      mocks.handlers?.onEvent({
        event: 'run.started',
        seq: 1,
        trace_id: 'trace',
        ts: 0,
        data: {},
      });
      mocks.handlers?.onSnapshot({});
    });

    await waitFor(() => expect(input.disabled).toBe(false));
    expect(useRunStore.getState().status).toBe('error');
    expect(useRunStore.getState().error?.code).toBe('RUN_STREAM_UNAVAILABLE');
  });

  it('refetches the run and session messages after an explicit replay gap', async () => {
    renderHarness();
    const input = screen.getByPlaceholderText('例如：华东上个月 GMV 多少？') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '查询 GMV' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(mocks.handlers).toBeDefined());

    await act(async () => {
      mocks.handlers?.onReplayGap?.({
        after_seq: 1,
        oldest_available_seq: 1048,
        latest_seq: 1247,
        advance_cursor: false,
      });
    });

    await waitFor(() => expect(useRunStore.getState().replayGap?.recovered).toBe(true));
    expect(mocks.getRunDoc).toHaveBeenCalledWith('trace');
    expect(mocks.listSessionMessages).toHaveBeenCalledWith('session');
    expect(useRunStore.getState().narration).toBe('Recovered answer');
  });
});
