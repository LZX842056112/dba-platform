// FinOps 优化建议 + 死循环告警。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Empty, PagePanel } from '../../components/PageBits';
import { applyRecommendation, getLoopAlerts, getRecommendations, rejectRecommendation } from '../../features/finops/api';

export function RecommendationsPage() {
  const qc = useQueryClient();
  const { data: recos } = useQuery({ queryKey: ['finops-recos'], queryFn: getRecommendations, refetchInterval: 30000 });
  const { data: alerts } = useQuery({ queryKey: ['finops-loop'], queryFn: getLoopAlerts, refetchInterval: 30000 });

  const apply = useMutation({
    mutationFn: ({ id, dryRun }: { id: string; dryRun: boolean }) => applyRecommendation(id, dryRun),
    onSettled: () => qc.invalidateQueries({ queryKey: ['finops-recos'] }),
  });
  const reject = useMutation({
    mutationFn: (id: string) => rejectRecommendation(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['finops-recos'] }),
  });

  const list = (recos ?? []) as Array<Record<string, unknown>>;
  const loopAlerts = (alerts ?? []) as Array<Record<string, unknown>>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <PagePanel title="优化建议">
        {list.length ? (
          <div style={{ flex: 1, overflow: 'auto' }}>
            <table className="data-table">
              <thead><tr><th>#</th><th>类型</th><th>描述</th><th>预估节省</th><th>操作</th></tr></thead>
              <tbody>
                {list.map((r, i) => (
                  <tr key={i}>
                    <td>{String(r.id ?? i + 1)}</td>
                    <td>{String(r.type ?? '—')}</td>
                    <td>{String(r.description ?? r.reason ?? '—')}</td>
                    <td>{String(r.estimated_saving_micro_usd ?? '—')}</td>
                    <td>
                      <button style={{ marginRight: 6 }} onClick={() => apply.mutate({ id: String(r.id), dryRun: true })}>试算</button>
                      <button onClick={() => reject.mutate(String(r.id))}>拒绝</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty text="暂无建议（需要更多成本数据）" />}
      </PagePanel>
      <PagePanel title="死循环告警">
        {loopAlerts.length ? (
          <table className="data-table">
            <thead><tr><th>id</th><th>trace_id</th><th>描述</th></tr></thead>
            <tbody>{loopAlerts.map((a, i) => <tr key={i}><td>{String(a.id ?? '—')}</td><td>{String(a.trace_id ?? '—')}</td><td>{String(a.message ?? '—')}</td></tr>)}</tbody>
          </table>
        ) : <Empty text="暂无死循环告警" />}
      </PagePanel>
    </div>
  );
}
