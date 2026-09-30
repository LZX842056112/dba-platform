// 发起提问 + 订阅 SSE 的编排钩子。
//
// 负责把 §9.3 的事件流接进 runStore，并处理：
//   * 懒建会话（首次提问时 POST /chat/sessions）；
//   * P0-7：先拿 ticket 再开 EventSource；
//   * 重连耗尽 → 用 run 快照兜底（避免永久停在「进行中」）。

import { useMutation } from '@tanstack/react-query';
import { useCallback, useEffect, useRef } from 'react';
import { openRunStream } from '../../../api/sse';
import { useRunStore } from '../store/runStore';
import { createSession, startQuery } from '../api/chatApi';

export interface UseChatRun {
  start: (question: string) => void;
  starting: boolean;
  error: Error | null;
}

export function useChatRun(): UseChatRun {
  const apply = useRunStore((state) => state.apply);
  const reset = useRunStore((state) => state.reset);
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
      reset();
    },
    onSuccess: (result) => {
      closeStreamRef.current = openRunStream(result.trace_id, result.stream_ticket, {
        onEvent: (event) => apply(event),
        // 重连耗尽的兜底：run_doc 快照（§9.3 断线重连降级）
        onSnapshot: () => {
          /* run_doc 无面板数据，仅提示；面板已由 delta 落地 */
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
      if (!question.trim()) return;
      mutation.mutate(question.trim());
    },
    [mutation],
  );

  return { start, starting: mutation.isPending, error: mutation.error };
}
