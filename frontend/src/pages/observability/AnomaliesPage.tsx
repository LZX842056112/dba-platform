// 异常告警：alert_event 列表 + 确认/解决。
//
// ★ 列名与枚举值统一取 features/observability/labels（与运行明细、运行详情同一套）。
//   原实现把 `alert_type`（alert_event 并无此字段）当类型列，fallback 后实际显示的是
//   severity，与「严重度」列重复。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Empty, PagePanel } from '../../components/PageBits';
import { ackAnomaly, getAnomalies, resolveAnomaly } from '../../features/observability/api';
import {
  PLACEHOLDER,
  alertCategoryLabel,
  alertSeverityMeta,
  alertStatusMeta,
} from '../../features/observability/labels';
import { formatNumber, formatTime } from '../../lib/format';

const NUM = 'num';

/** 观察值可能是成本（微美元）也可能是比率，统一保留 2 位小数，不擅自换算单位。 */
function numText(value: unknown): string {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return formatNumber(Math.round(n * 100) / 100);
}

export function AnomaliesPage() {
  const qc = useQueryClient();
  const { data, isPending } = useQuery({
    queryKey: ['obs-anomalies'],
    queryFn: getAnomalies,
    refetchInterval: 15000,
  });
  const anomalies = data ?? [];

  const act = useMutation({
    mutationFn: ({ id, kind }: { id: string; kind: 'ack' | 'resolve' }) =>
      kind === 'ack' ? ackAnomaly(id) : resolveAnomaly(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['obs-anomalies'] }),
  });

  if (anomalies.length === 0) {
    return (
      <PagePanel title="异常告警">
        <Empty text={isPending ? '正在加载…' : '暂无异常（需更多 Run + anomaly_scan 才产出）'} />
      </PagePanel>
    );
  }

  return (
    <PagePanel title="异常告警">
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>检测时间</th>
              <th>类别</th>
              <th>严重度</th>
              <th>指标</th>
              <th className={NUM}>观察值 / 基线</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {anomalies.map((alert, index) => {
              const id = alert.id;
              const severity = alertSeverityMeta(alert.severity);
              const status = alertStatusMeta(alert.status);
              const zscore = alert.robust_zscore;
              return (
                <tr key={Number.isFinite(id) ? id : index}>
                  <td>{Number.isFinite(id) ? id : PLACEHOLDER}</td>
                  <td>{formatTime(alert.detected_at)}</td>
                  <td>{alertCategoryLabel(alert.category)}</td>
                  <td>
                    <span className={`badge ${severity.badgeClass}`}>{severity.label}</span>
                  </td>
                  <td title={alert.metric ?? ''}>{alert.metric ?? PLACEHOLDER}</td>
                  <td
                    className={NUM}
                    title={zscore == null ? undefined : `robust z = ${numText(zscore)}`}
                  >
                    {numText(alert.observed_value)} / {numText(alert.baseline_value)}
                  </td>
                  <td>
                    <span className={`badge ${status.badgeClass}`}>{status.label}</span>
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button
                      type="button"
                      style={{ marginRight: 6 }}
                      disabled={act.isPending || alert.status !== 'open'}
                      onClick={() => act.mutate({ id: String(alert.id), kind: 'ack' })}
                    >
                      确认
                    </button>
                    <button
                      type="button"
                      disabled={
                        act.isPending || alert.status === 'resolved' || alert.status === 'ignored'
                      }
                      onClick={() => act.mutate({ id: String(alert.id), kind: 'resolve' })}
                    >
                      解决
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </PagePanel>
  );
}
