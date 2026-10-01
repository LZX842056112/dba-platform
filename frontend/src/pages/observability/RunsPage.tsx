// Run 列表。

import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Empty, PagePanel } from '../../components/PageBits';
import { getRuns } from '../../features/observability/api';

export function RunsPage() {
  const { data } = useQuery({ queryKey: ['obs-runs'], queryFn: getRuns, refetchInterval: 10000 });
  const runs = (data ?? []) as Array<Record<string, unknown>>;

  if (runs.length === 0) return <PagePanel title="Run 列表"><Empty text="暂无 Run（去对话页提问后这里会有记录）" /></PagePanel>;

  const cols = ['trace_id', 'module', 'status', 'latency_ms', 'error_code'];

  return (
    <PagePanel title="Run 列表（≤200）">
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>{cols.map((c) => <th key={c}>{c}</th>)}<th>操作</th></tr>
          </thead>
          <tbody>
            {runs.map((r, i) => (
              <tr key={i}>
                {cols.map((c) => <td key={c}>{String(r[c] ?? '—')}</td>)}
                <td>
                  <Link to={`/observability/runs/${encodeURIComponent(String(r.trace_id))}`}>详情</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PagePanel>
  );
}
