// 运行详情：基本信息 + span 列表（自愈痕迹可见于重试/回退标记）。
//
// ★ 口径：run_doc 的顶层字段并非总是存在（只落了 span 的 Run 只有 trace_id + spans）。
//   因此基本信息区按字段有无逐项渲染，缺的整项不显示，也不用 span 数据反推成本。
// ★ 类型：RunDoc / RunSpan 由 features/observability/api 声明，页面不再做类型断言。

import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { Empty, PagePanel } from '../../components/PageBits';
import { getRunDetail, type RunSpan } from '../../features/observability/api';
import {
  PLACEHOLDER,
  moduleLabel,
  runStatusMeta,
  spanKindLabel,
  spanStatusMeta,
} from '../../features/observability/labels';
import { formatMicroUsd, formatMs, formatTime } from '../../lib/format';

const NUM = 'num';

/** 按 parent_span_id 计算层级，用于缩进；遇到断链或环就停止（最多 6 层）。 */
function withDepth(spans: RunSpan[]): Array<{ span: RunSpan; depth: number }> {
  const byId = new Map<string, RunSpan>();
  for (const span of spans) {
    if (span.span_id) byId.set(span.span_id, span);
  }
  return spans.map((span) => {
    let depth = 0;
    let current = span;
    const seen = new Set<string>();
    while (depth < 6) {
      const parentId = current.parent_span_id;
      if (!parentId || seen.has(parentId)) break;
      seen.add(parentId);
      const parent = byId.get(parentId);
      if (!parent) break;
      depth += 1;
      current = parent;
    }
    return { span, depth };
  });
}

export function RunDetailPage() {
  const { traceId } = useParams();
  const { data, isPending } = useQuery({
    queryKey: ['obs-run', traceId],
    queryFn: () => getRunDetail(traceId as string),
    enabled: Boolean(traceId),
  });

  const doc = data ?? {};
  const spans = doc.spans ?? [];
  const rows = withDepth(spans);
  const status = doc.status === undefined ? null : runStatusMeta(doc.status);

  // 基本信息：只收录真实存在的字段
  const facts: Array<{ label: string; value: string }> = [];
  if (doc.module !== undefined) facts.push({ label: '模块', value: moduleLabel(doc.module) });
  if (doc.started_at !== undefined) {
    facts.push({ label: '开始时间', value: formatTime(doc.started_at) });
  }
  if (typeof doc.latency_ms === 'number') {
    facts.push({ label: '总时延', value: formatMs(doc.latency_ms) });
  }
  if (typeof doc.cost_micro_usd === 'number') {
    facts.push({ label: '成本', value: formatMicroUsd(doc.cost_micro_usd) });
  }
  if (spans.length > 0) facts.push({ label: 'Span 数', value: String(spans.length) });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Link to="/observability/runs">← 返回运行明细</Link>
        <span className="dim" style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
          trace {traceId ?? PLACEHOLDER}
        </span>
        {status ? <span className={`badge ${status.badgeClass}`}>{status.label}</span> : null}
      </div>

      {facts.length > 0 ? (
        <PagePanel title="基本信息">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 28px' }}>
            {facts.map((fact) => (
              <div key={fact.label} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span className="dim" style={{ fontSize: 11 }}>
                  {fact.label}
                </span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13 }}>{fact.value}</span>
              </div>
            ))}
          </div>
        </PagePanel>
      ) : null}

      <PagePanel title="Span 列表">
        {rows.length > 0 ? (
          <>
            <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>开始时间</th>
                    <th className={NUM}>耗时</th>
                    <th>状态</th>
                    <th>错误</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ span, depth }, index) => {
                    const spanStatus = spanStatusMeta(span.status);
                    return (
                      <tr key={span.span_id || index}>
                        <td>
                          <span
                            style={{ paddingLeft: depth * 14, display: 'inline-block' }}
                            title={`span ${span.span_id || '—'} · parent ${span.parent_span_id ?? '—'}`}
                          >
                            {span.name ?? PLACEHOLDER}
                          </span>
                        </td>
                        <td>{spanKindLabel(span.kind)}</td>
                        <td>{formatTime(span.start_ms)}</td>
                        <td className={NUM}>{formatMs(span.duration_ms)}</td>
                        <td>
                          <span className={`badge ${spanStatus.badgeClass}`}>{spanStatus.label}</span>
                        </td>
                        <td title={span.error_message ?? undefined}>
                          {span.error_type ?? PLACEHOLDER}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="dim" style={{ margin: '6px 0 0', fontSize: 11 }}>
              按落库顺序列出；左侧缩进表示父子层级（依据 parent_span_id）。
            </p>
          </>
        ) : (
          <Empty
            text={
              isPending
                ? '正在加载…'
                : '暂无 span 数据（该 Run 的 span 尚未落 Mongo 或已过期）'
            }
          />
        )}
      </PagePanel>
    </div>
  );
}
