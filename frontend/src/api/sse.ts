// SSE 客户端（★ 对齐《设计方案 v2》§9.3 断线续传）。
//
// v1 的三个硬伤（本实现已修正）：
//   ① EventSource 无法携带 Authorization 头 → 用一次性 stream_ticket（query 参数）建连；
//   ② 客户端手写 lastId 从未被使用 → v2 改为**服务端写 `id: {seq}`**，
//      浏览器自动带 Last-Event-ID，客户端不再手写（见后端 api/v1/stream.py）；
//   ③ ★ 后端 `build_sse_frames` 写的是**命名事件**（`event: run.started`），
//      因此**不能用 `es.onmessage`**——`onmessage` 只接收默认（无 `event:` 字段）的
//      message 事件。必须对每个事件名单独 `addEventListener`。
//
// 另外：SSE 帧的 `data:` 字段只装事件 payload（不是完整信封），
// 信封的 `event` / `seq` 由 SSE 的 `event:` 名与 `id:` 还原，`trace_id` 由调用方已知，
// `ts` 用浏览器接收时刻（墙钟）。见 §9.3 的「统一信封」。
//
// 重连耗尽后**降级为一次性拉取终态**（GET /chat/runs/{trace_id}），
// 避免用户永远停在「进行中」。

import type { EventEnvelope, EventType } from '../types/events';
import { isTerminal } from '../types/events';
import type { ReplayGapData } from '../types/events';
import { getRunDoc } from '../features/chat/api/chatApi';
import { renewStreamTicket } from '../features/chat/api/chatApi';

export interface RunStreamHandlers {
  onEvent: (event: EventEnvelope) => void;
  /** 重连耗尽后的降级：一次性取回终态。 */
  onSnapshot: (snapshot: unknown) => void;
  onOpen?: () => void;
  onError?: () => void;
  onReplayGap?: (gap: ReplayGapData) => void;
}

const MAX_RETRY = 5;

/** 后端会以命名事件推送的全部事件名（必须逐个 addEventListener）。 */
const NAMED_EVENTS: EventType[] = [
  'run.started',
  'agent.step.started',
  'agent.step.delta',
  'agent.step.finished',
  'agent.step.retrying',
  'sql.generated',
  'sql.validation.failed',
  'sql.retry.resolved',
  'sql.executing',
  'sql.executed',
  'dashboard.spec.delta',
  'dashboard.spec.ready',
  'narration.delta',
  'budget.warning',
  'budget.downgraded',
  'budget.blocked',
  'run.finished',
  'run.error',
  'run.aborted',
  'replay.gap',
  'heartbeat',
];

function baseUrl(): string {
  return (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1';
}

function parseData(raw: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/**
 * 打开一次 Run 的事件流。返回关闭函数。
 *
 * @param traceId Run 的 trace_id
 * @param ticket  一次性 stream_ticket（由 POST /chat/sessions/{sid}/query 下发）
 */
export function openRunStream(
  traceId: string,
  ticket: string,
  handlers: RunStreamHandlers,
): () => void {
  let es: EventSource | null = null;
  let retry = 0;
  let closedByUs = false;
  let lastSeq = 0;
  let retryTimer: number | null = null;

  const loadSnapshot = (): void => {
    closedByUs = true;
    void getRunDoc(traceId)
      .then(handlers.onSnapshot)
      .catch(() => handlers.onSnapshot({}));
  };

  const connect = (currentTicket: string): void => {
    // ★ 不传 JWT（EventSource 不支持自定义请求头），改用一次性 ticket
    const cursor = lastSeq > 0 ? `&last_event_id=${lastSeq}` : '';
    const url = `${baseUrl()}/stream/runs/${encodeURIComponent(traceId)}?ticket=${encodeURIComponent(currentTicket)}${cursor}`;
    es = new EventSource(url);

    es.onopen = () => {
      retry = 0;
      handlers.onOpen?.();
    };

    const makeHandler =
      (name: EventType) =>
      (message: MessageEvent<string>): void => {
        const parsedSeq = Number(message.lastEventId);
        // 心跳帧不带 `id:`，per SSE 规范 lastEventId 会沿用上一个 id；
        // 由 runStore 的 `seq <= lastSeq` 幂等判定自然丢弃，这里无需特殊处理。
        const seq = Number.isFinite(parsedSeq) && parsedSeq > 0 ? parsedSeq : lastSeq;
        const env: EventEnvelope = {
          event: name,
          seq,
          trace_id: traceId,
          ts: Date.now(),
          data: parseData(message.data),
        };
        lastSeq = Math.max(lastSeq, seq);
        if (name !== 'heartbeat') handlers.onEvent(env);
        if (isTerminal(name)) {
          closedByUs = true;
          es?.close();
        }
      };

    // ★ 关键：命名事件必须逐个注册监听器
    for (const name of NAMED_EVENTS) {
      if (name === 'replay.gap') {
        es.addEventListener(name, ((message: MessageEvent<string>) => {
          const parsed = parseData(message.data);
          const oldest = parsed.oldest_available_seq;
          const latest = parsed.latest_seq;
          if (
            typeof parsed.after_seq !== 'number' ||
            (oldest !== null && typeof oldest !== 'number') ||
            (latest !== null && typeof latest !== 'number') ||
            typeof parsed.advance_cursor !== 'boolean'
          ) return;
          const data: ReplayGapData = {
            after_seq: parsed.after_seq,
            oldest_available_seq: oldest,
            latest_seq: latest,
            advance_cursor: parsed.advance_cursor,
          };
          if (data.advance_cursor && typeof data.latest_seq === 'number') {
            lastSeq = Math.max(lastSeq, data.latest_seq);
          }
          handlers.onReplayGap?.(data);
        }) as EventListener);
      } else {
        es.addEventListener(name, makeHandler(name) as EventListener);
      }
    }
    // 兜底：后端若以默认 message 事件推送，也能收到（按未命名处理，仅推进水位）
    es.onmessage = (message: MessageEvent<string>) => {
      const env: EventEnvelope = {
        event: 'heartbeat',
        seq: lastSeq,
        trace_id: traceId,
        ts: Date.now(),
        data: parseData(message.data),
      };
      handlers.onEvent(env);
    };

    es.onerror = () => {
      handlers.onError?.();
      es?.close();
      if (closedByUs) return;
      if (retry >= MAX_RETRY) return loadSnapshot();
      retry += 1;
      const delay = Math.min(1000 * 2 ** retry, 15000);
      retryTimer = window.setTimeout(() => {
        // 新建 EventSource 不会继承旧实例的 Last-Event-ID；显式传水位并换新 ticket。
        void renewStreamTicket(traceId)
          .then(({ stream_ticket }) => {
            if (!closedByUs) connect(stream_ticket);
          })
          .catch(loadSnapshot);
      }, delay);
    };
  };

  connect(ticket);

  return () => {
    closedByUs = true;
    if (retryTimer !== null) window.clearTimeout(retryTimer);
    es?.close();
    es = null;
  };
}
