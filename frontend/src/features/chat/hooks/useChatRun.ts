// 发起提问 + 订阅 SSE 的编排钩子。
//
// 负责把 §9.3 的事件流接进 runStore，并处理：
//   * 懒建会话（首次提问时 POST /chat/sessions）；
//   * P0-7：先拿 ticket 再开 EventSource；
//   * 重连耗尽 → 用 run 快照兜底（避免永久停在「进行中」）。

import { useMutation } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { openRunStream } from '../../../api/sse';
import { useRunStore } from '../store/runStore';
import {
  createSession,
  getRunDoc,
  listSessionMessages,
  startQuery,
} from '../api/chatApi';
import { isTerminal } from '../../../types/events';

export interface UseChatRun {
  start: (question: string) => void;
  busy: boolean;
  error: Error | null;
}

export function useChatRun(): UseChatRun {
  const apply = useRunStore((state) => state.apply);
  const reset = useRunStore((state) => state.reset);
  const status = useRunStore((state) => state.status);
  const [streaming, setStreaming] = useState(false);
  const sessionRef = useRef<string | null>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);

  const mutation = useMutation({
    mutationFn: async (question: string) => {
      if (!sessionRef.current) {
        const session = await createSession();
        sessionRef.current = session.session_id;
      }
      return startQuery(sessionRef.current, question);
    },
    onMutate: () => {
      closeStreamRef.current?.();
      closeStreamRef.current = null;
      setStreaming(false);
      reset();
    },
    onSuccess: (result) => {
      // POST 只负责创建 Run；SSE 完成前输入仍需锁定，任意终态或快照回退时再恢复。
      setStreaming(true);
      closeStreamRef.current = openRunStream(result.trace_id, result.stream_ticket, {
        onEvent: (event) => {
          apply(event);
          if (isTerminal(event.event)) setStreaming(false);
        },
        onReplayGap: (gap) => {
          const store = useRunStore.getState();
          store.markReplayGap({
            afterSeq: gap.after_seq,
            oldestAvailableSeq: gap.oldest_available_seq,
            latestSeq: gap.latest_seq,
          });
          const sessionId = sessionRef.current;
          if (!sessionId) {
            store.recoverReplayGap(0);
            return;
          }
          // SSE 缺口无法重建历史 delta；span 树和已落库的 assistant 消息作为权威快照。
          void Promise.allSettled([getRunDoc(result.trace_id), listSessionMessages(sessionId)]).then(
            ([runDocResult, messagesResult]) => {
              const runDoc = runDocResult.status === 'fulfilled' ? runDocResult.value : {};
              const messages = messagesResult.status === 'fulfilled' ? messagesResult.value : [];
              const spans = Array.isArray(runDoc.spans) ? runDoc.spans : [];
              const answer = [...messages]
                .reverse()
                .find((message) => message.role === 'assistant' && message.trace_id === result.trace_id)
                ?.content;
              useRunStore.getState().recoverReplayGap(spans.length, answer);
            },
          );
        },
        // 重连耗尽的兜底：run_doc 快照（§9.3 断线重连降级）
        onSnapshot: () => {
          // run_doc 不含运行终态；重连耗尽时显式结束本地运行，防止输入永久锁定。
          const current = useRunStore.getState();
          if (current.status === 'running') {
            apply({
              event: 'run.error',
              seq: current.lastSeq + 1,
              trace_id: result.trace_id,
              ts: Date.now(),
              data: {
                code: 'RUN_STREAM_UNAVAILABLE',
                message: '事件流中断，未能恢复运行终态。请检查 Run 详情后重试。',
                retryable: true,
              },
            });
          }
          setStreaming(false);
        },
      });
    },
  });

  // 卸载时关闭连接
  useEffect(
    () => () => {
      closeStreamRef.current?.();
      closeStreamRef.current = null;
    },
    [],
  );

  const start = useCallback(
    (question: string) => {
      if (!question.trim() || mutation.isPending || streaming || status === 'running') return;
      mutation.mutate(question.trim());
    },
    [mutation, status, streaming],
  );

  return {
    start,
    busy: mutation.isPending || streaming || status === 'running',
    error: mutation.error,
  };
}
