// 异常事件列表 + 确认/解决。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Empty, PagePanel } from '../../components/PageBits';
import { ackAnomaly, getAnomalies, resolveAnomaly } from '../../features/observability/api';

export function AnomaliesPage() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ['obs-anomalies'], queryFn: getAnomalies, refetchInterval: 15000 });
  const anomalies = (data ?? []) as Array<Record<string, unknown>>;

  const act = useMutation({
    mutationFn: ({ id, kind }: { id: string; kind: 'ack' | 'resolve' }) =>
      kind === 'ack' ? ackAnomaly(id) : resolveAnomaly(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['obs-anomalies'] }),
  });

  if (anomalies.length === 0) {
    return <PagePanel title="异常事件"><Empty text="暂无异常（需更多 Run + anomaly_scan 才产出）" /></PagePanel>;
  }

  return (
    <PagePanel title="异常事件">
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr><th>id</th><th>类型</th><th>严重度</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {anomalies.map((a, i) => (
              <tr key={i}>
                <td>{String(a.id ?? '—')}</td>
                <td>{String(a.alert_type ?? a.severity ?? '—')}</td>
                <td>{String(a.severity ?? '—')}</td>
                <td>{String(a.status ?? '—')}</td>
                <td>
                  <button style={{ marginRight: 6 }} onClick={() => act.mutate({ id: String(a.id), kind: 'ack' })}>确认</button>
                  <button onClick={() => act.mutate({ id: String(a.id), kind: 'resolve' })}>解决</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PagePanel>
  );
}
