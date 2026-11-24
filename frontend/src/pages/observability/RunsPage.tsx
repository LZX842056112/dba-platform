// 运行明细：最近一批 Run 的中文表格。
//
// ★ 时间、时延、成本、状态一律走 lib/format 与 features/observability/labels，
//   与流程监控、Run 详情保持同一套格式；取不到的字段显示「—」，不补零。

import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Empty, PagePanel } from '../../components/PageBits';
import { getRuns } from '../../features/observability/api';
import { PLACEHOLDER, moduleLabel, runStatusMeta } from '../../features/observability/labels';
import { formatMicroUsd, formatMs, formatTime } from '../../lib/format';

/** 数值列的右侧对齐（见 global.css .data-table .num）。 */
const NUM = 'num';

export function RunsPage() {
  const { data, isPending } = useQuery({
    queryKey: ['obs-runs'],
    queryFn: getRuns,
    refetchInterval: 10000,
  });
  const runs = data ?? [];

  if (runs.length === 0) {
    return (
      <PagePanel title="运行明细（最近 ≤200 条）">
        <Empty text={isPending ? '正在加载…' : '暂无运行记录（去对话页提问后这里会有记录）'} />
      </PagePanel>
    );
  }

  return (
    <PagePanel title="运行明细（最近 ≤200 条）">
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>开始时间</th>
              <th>模块</th>
              <th>状态</th>
              <th className={NUM}>时延</th>
              <th className={NUM}>成本</th>
              <th>错误码</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run, index) => {
              const traceId = run.trace_id;
              const status = runStatusMeta(run.status);
              return (
                <tr key={traceId || index}>
                  <td>
                    <span className="mono-short" title={traceId || '无 trace_id'}>
                      {traceId ? traceId.slice(0, 8) : PLACEHOLDER}
                    </span>
                  </td>
                  <td>{formatTime(run.started_at)}</td>
                  <td>{moduleLabel(run.module)}</td>
                  <td>
                    <span className={`badge ${status.badgeClass}`}>{status.label}</span>
                  </td>
                  <td className={NUM}>{formatMs(run.latency_ms)}</td>
                  <td className={NUM}>{formatMicroUsd(run.cost_micro_usd)}</td>
                  <td title={run.error_code ?? undefined}>{run.error_code ?? PLACEHOLDER}</td>
                  <td>
                    <Link to={`/observability/runs/${encodeURIComponent(traceId)}`}>详情</Link>
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
