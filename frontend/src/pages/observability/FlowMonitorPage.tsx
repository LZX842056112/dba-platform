// 流程监控大屏（/observability/flow）。
//
// 布局参考大屏范式：左「模块工位」、中「七步流水线 / KPI / 成本趋势」、右「实时运行流」。
//
// ★ 数据口径（只读现有接口，不新增后端）：
//   - /obs/overview  → KPI 卡、日成本时序、Top Agent（统计区间由顶部下拉控制）
//   - /obs/runs      → 最近 ≤200 条 Run（后端固定 limit=200，按 started_at 倒序）
//   - /obs/topology  → Agent/工具 span 计数，用于流水线节点着色
//   任何取不到的字段一律显示「—」：不补零、不推算、不造演示数（与全站「诚实空态」一致）。
//
// ★ 状态 / 模块 / 占位符的词表统一取自 features/observability/labels，
//   与「运行明细」「运行详情」共用，避免同一字段在不同页面写成不同中文。

import { Fragment, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';

import { EchartsBase } from '../../components/charts/EchartsBase';
import { STEP_ORDER, stepLabel } from '../../features/chat/constants';
import { useCountUp } from '../../features/dashboard/hooks/useCountUp';
import { getOverview, getRuns, getTopology } from '../../features/observability/api';
import {
  FAILED_RUN_STATUS,
  MODULES,
  moduleLabel,
  runStatusMeta,
  type RunTone,
} from '../../features/observability/labels';
import type { EChartsCoreOption } from '../../lib/echarts';
import { formatMicroUsd, formatMs } from '../../lib/format';

import '../../styles/cockpit.css';
import '../../styles/flow-monitor.css';

const intFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });

/**
 * 流水线节点短标签：中栏在 1150px 视口下每节点仅约 55px，
 * 全名（「SQL 校验」「可视化编排」）会被折断；完整步骤名放 title 里。
 */
const FLOW_LABELS: Record<string, string> = {
  intent: '意图',
  schema_link: '链接',
  sql_gen: '生成',
  sql_guard: '校验',
  sql_exec: '执行',
  visual: '编排',
  narrator: '解读',
};

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? Math.round((sorted[mid - 1] + sorted[mid]) / 2) : sorted[mid];
}

/** ISO 时间串 → HH:mm:ss（解析失败返回占位符，不猜）。 */
function clockOf(input: string | null | undefined): string {
  if (!input) return '--:--:--';
  const date = new Date(input);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

/** ISO 日粒度串 → MM-DD。 */
function dayOf(ts: string): string {
  return ts.length >= 10 ? ts.slice(5, 10) : ts;
}

function useClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now.toLocaleTimeString('zh-CN', { hour12: false });
}

/** 翻牌数字：目标值来自接口，动画只做过渡；target 缺失时显示「—」。 */
function CountValue({
  value,
  format,
  unit,
}: {
  value: number | undefined;
  format: (n: number) => string;
  unit?: string;
}) {
  const animated = useCountUp(value ?? 0, 700);
  return (
    <span className="fm-kpi__value">
      {value === undefined ? '—' : format(animated)}
      {value !== undefined && unit ? <span className="fm-kpi__unit">{unit}</span> : null}
    </span>
  );
}

export function FlowMonitorPage() {
  const [hours, setHours] = useState(24);
  const [moduleFilter, setModuleFilter] = useState<string | null>(null);
  const clock = useClock();

  const overview = useQuery({
    queryKey: ['obs-overview', hours],
    queryFn: () => getOverview(hours),
    refetchInterval: 15000,
  });
  const runsQuery = useQuery({ queryKey: ['obs-runs'], queryFn: getRuns, refetchInterval: 10000 });
  const topoQuery = useQuery({
    queryKey: ['obs-topology'],
    queryFn: getTopology,
    refetchInterval: 30000,
  });

  const kpi = overview.data?.kpi_cards;
  const successRatePct =
    kpi?.success_rate?.value === undefined ? undefined : kpi.success_rate.value * 100;
  const runs = runsQuery.data ?? [];

  // ── 模块工位：按 module 聚合最近一批 Run（样本量在标题上写明）────────
  const moduleStats = useMemo(() => {
    const buckets = new Map<string, { runs: number; fails: number; running: number; lat: number[] }>();
    const emptyBucket = () => ({ runs: 0, fails: 0, running: 0, lat: [] as number[] });
    for (const key of Object.keys(MODULES)) {
      buckets.set(key, emptyBucket());
    }
    for (const run of runs) {
      const moduleKey = run.module ?? '';
      const bucket = buckets.get(moduleKey) ?? emptyBucket();
      bucket.runs += 1;
      if (FAILED_RUN_STATUS.has(run.status ?? '')) bucket.fails += 1;
      if (run.status === 'running') bucket.running += 1;
      if (typeof run.latency_ms === 'number') bucket.lat.push(run.latency_ms);
      buckets.set(moduleKey, bucket);
    }
    const total = runs.length;
    return [...buckets.entries()]
      .map(([key, b]) => ({
        key,
        label: moduleLabel(key),
        runs: b.runs,
        fails: b.fails,
        running: b.running,
        p50: b.lat.length > 0 ? median(b.lat) : null,
        share: total > 0 ? b.runs / total : 0,
      }))
      .sort((a, b) => b.runs - a.runs || a.key.localeCompare(b.key));
  }, [runs]);

  const activeModuleLabel = moduleFilter ? moduleLabel(moduleFilter, true) : '全部模块';

  const feedRuns = useMemo(
    () => (moduleFilter ? runs.filter((r) => r.module === moduleFilter) : runs).slice(0, 60),
    [runs, moduleFilter],
  );

  // ── 七步流水线：步骤定义来自 §4.1，节点计数取 topology 的 Agent span ──
  // 匹配前把两侧都归一成纯小写字母数字，`schema_link` 才能对上 `SchemaLinkAgent`。
  const stepNodes = useMemo(() => {
    const nodes = topoQuery.data?.nodes ?? [];
    const norm = (text: string) => text.toLowerCase().replace(/[^a-z0-9]/g, '');
    return STEP_ORDER.map((step) => {
      const needle = norm(step);
      const hit = nodes.find((n) => norm(`${n.node_id} ${n.name}`).includes(needle));
      return {
        step,
        label: FLOW_LABELS[step] ?? stepLabel(step),
        fullLabel: stepLabel(step),
        spans: hit ? hit.span_count : null,
        errors: hit ? hit.error_count : 0,
        agent: hit ? hit.name : null,
      };
    });
  }, [topoQuery.data]);

  // ── 成本趋势：metric_daily 按天可能有多行（agent/model 维度），先按 ts 求和 ──
  const trend = useMemo(() => {
    const series = overview.data?.trend?.series ?? [];
    const byTs = new Map<string, number>();
    for (const row of series) {
      if (!row.ts) continue;
      byTs.set(row.ts, (byTs.get(row.ts) ?? 0) + row.value);
    }
    const entries = [...byTs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    const totalMicro = entries.reduce((sum, [, micro]) => sum + micro, 0);
    return {
      labels: entries.map(([ts]) => dayOf(ts)),
      values: entries.map(([, micro]) => micro / 1_000_000),
      totalMicro,
    };
  }, [overview.data]);

  const trendOption = useMemo<EChartsCoreOption>(() => {
    const lastIndex = trend.values.length - 1;
    return {
      grid: { left: 4, right: 18, top: 12, bottom: 6, containLabel: true },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: unknown) => `$${Number(value).toFixed(4)}`,
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: trend.labels,
        axisTick: { show: false },
        axisLabel: { fontSize: 10, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        splitNumber: 3,
        axisLabel: { fontSize: 10, formatter: (value: number) => `$${value}` },
      },
      series: [
        {
          name: '日成本',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: trend.values,
          lineStyle: { width: 1.6, color: '#22d3ee' },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(34, 211, 238, 0.34)' },
                { offset: 1, color: 'rgba(34, 211, 238, 0)' },
              ],
            },
          },
        },
        {
          name: '末端',
          type: 'scatter',
          symbolSize: 7,
          silent: true,
          data: trend.values.map((value, index) => (index === lastIndex ? value : null)),
          itemStyle: { color: '#7ef0ff', shadowBlur: 10, shadowColor: '#22d3ee' },
        },
      ],
    };
  }, [trend]);

  // top_agents 已由接口给出结构化字段（TopAgent），页面不再逐个转换
  const topAgents = overview.data?.top_agents ?? [];
  const maxAgentRuns = Math.max(1, ...topAgents.map((agent) => agent.runs));

  const hasError = overview.isError || runsQuery.isError || topoQuery.isError;

  return (
    <div className="fm-page">
      <div className="cockpit">
        <div className="cockpit__bg" aria-hidden />
        <header className="cockpit__head">
          <span className="cockpit__title">流程监控 · PIPELINE MONITOR</span>
          <span className="fm-head__right">
            <span className="fm-live">
              <i aria-hidden />
              LIVE
            </span>
            <select
              className="fm-select"
              value={hours}
              onChange={(event) => setHours(Number(event.target.value))}
              aria-label="统计区间"
            >
              <option value={24}>近 24 小时</option>
              <option value={720}>近 30 天</option>
              <option value={2160}>近 90 天</option>
            </select>
            <span className="cockpit__meta">{clock}</span>
          </span>
        </header>

        <div className="cockpit__body">
          <div className="fm-stack">
            {hasError ? (
              <div className="fm-alert" role="alert">
                部分观测指标读取失败：后端不可用或接口降级，下方对应数值显示为「—」。
              </div>
            ) : null}

            <div className="fm-grid">
              {/* ── 左栏：模块工位 + Top Agent ── */}
              <section className="panel-card panel-card--neon fm-panel fm-rail">
                <div className="fm-panel__head">
                  <span className="fm-panel__title">模块工位</span>
                  <button
                    type="button"
                    className="fm-chip"
                    data-on={moduleFilter === null}
                    onClick={() => setModuleFilter(null)}
                  >
                    全部
                  </button>
                </div>
                <div className="fm-rail__list">
                  {moduleStats.map((m) => {
                    const tone: RunTone | undefined =
                      m.running > 0 ? 'run' : m.fails > 0 ? 'err' : m.runs > 0 ? 'ok' : undefined;
                    return (
                      <button
                        key={m.key}
                        type="button"
                        className="fm-station"
                        data-active={moduleFilter === m.key}
                        onClick={() => setModuleFilter((cur) => (cur === m.key ? null : m.key))}
                        title={`${m.label}：最近 ${m.runs} 条 Run`}
                      >
                        <span className="fm-station__top">
                          <span className="fm-station__dot" data-tone={tone} />
                          <span className="fm-station__name">{m.label}</span>
                          <span className="fm-station__value">{m.runs}</span>
                        </span>
                        <span className="fm-station__sub">
                          {m.running > 0 ? `运行中 ${m.running} · ` : ''}
                          失败 {m.fails} · P50 {m.p50 === null ? '—' : formatMs(m.p50)}
                        </span>
                        <span className="fm-station__bar">
                          <i style={{ width: `${Math.round(m.share * 100)}%` }} />
                        </span>
                      </button>
                    );
                  })}
                </div>

                <div className="fm-rail__sep" />

                <div className="fm-panel__head">
                  <span className="fm-panel__title">Top Agent</span>
                  <span className="fm-panel__hint">按调用量</span>
                </div>
                {topAgents.length === 0 ? (
                  <div className="fm-empty">
                    {overview.isPending ? '正在加载…' : '所选区间没有带 Agent 标识的运行。'}
                  </div>
                ) : (
                  <div className="rank-list">
                    {topAgents.map((agent, index) => (
                      <div className="fm-rank" key={agent.agent_uid}>
                        <span className={`rank-no${index === 0 ? ' rank-no--top' : ''}`}>
                          {index + 1}
                        </span>
                        <span
                          className="fm-rank__name"
                          title={`${agent.agent_uid} · 成功率 ${(agent.success_rate * 100).toFixed(1)}%`}
                        >
                          {agent.agent_uid}
                        </span>
                        <span className="fm-rank__value">{agent.runs}</span>
                        <span className="rank-bar">
                          <i
                            className="rank-bar__fill"
                            style={{ width: `${Math.round((agent.runs / maxAgentRuns) * 100)}%` }}
                          />
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                <p className="fm-note">
                  占比按最近 {runs.length} 条 Run 计算；取不到的字段显示「—」。
                </p>
              </section>

              {/* ── 中上：七步流水线 ── */}
              <section className="panel-card panel-card--neon fm-panel fm-flow-panel">
                <div className="fm-panel__head">
                  <span className="fm-panel__title">七步流水线</span>
                  <span className="fm-panel__hint">步骤取 §4.1 · 节点计数取 Agent span</span>
                </div>
                <div className="fm-flow">
                  {stepNodes.map((node, index) => {
                    // 环色只表示「有无 span 流量」；错误数用右上角红角标单独标，避免个位数
                    // 错误把整条流水线染红（观感误导）。
                    const idle = node.spans === null || node.spans === 0;
                    return (
                      <Fragment key={node.step}>
                        {index > 0 ? <span className="fm-flow__link" aria-hidden /> : null}
                        <div
                          className="fm-node"
                          data-state={idle ? 'idle' : 'ok'}
                          title={`${node.fullLabel}（${node.step}）· ${node.agent ?? '未匹配到同名 span'}${
                            node.errors > 0 ? ` · 错误 span ${node.errors}` : ''
                          }`}
                        >
                          <div className="fm-node__ring">
                            {index + 1}
                            {node.errors > 0 ? <span className="fm-node__err">{node.errors}</span> : null}
                          </div>
                          <div className="fm-node__label">{node.label}</div>
                          <div className="fm-node__meta">{node.spans === null ? '—' : node.spans}</div>
                        </div>
                      </Fragment>
                    );
                  })}
                </div>
              </section>

              {/* ── 中中：KPI ── */}
              <div className="fm-kpi">
                <div className="fm-kpi__card fm-kpi__card--hero" title="区间成本（派生量，按 metric_daily 汇总）">
                  <span className="fm-kpi__label">区间成本</span>
                  <CountValue value={kpi?.cost_today_micro?.value} format={formatMicroUsd} />
                </div>
                <div className="fm-kpi__card">
                  <span className="fm-kpi__label">区间内运行</span>
                  <CountValue value={kpi?.total_runs?.value} format={intFormat.format} />
                </div>
                <div className="fm-kpi__card">
                  <span className="fm-kpi__label">当前运行中</span>
                  <CountValue value={kpi?.running_now?.value} format={intFormat.format} />
                </div>
                <div className="fm-kpi__card">
                  <span className="fm-kpi__label">成功率</span>
                  <CountValue
                    value={successRatePct}
                    format={(n) => n.toFixed(1)}
                    unit="%"
                  />
                </div>
                <div className="fm-kpi__card fm-kpi__card--danger">
                  <span className="fm-kpi__label">静默失败</span>
                  <CountValue value={kpi?.silent_failures?.value} format={intFormat.format} />
                </div>
              </div>

              {/* ── 中下：成本趋势 ── */}
              <section className="panel-card panel-card--neon fm-panel fm-trend">
                <div className="fm-panel__head">
                  <span className="fm-panel__title">成本趋势</span>
                  <span className="fm-panel__hint">
                    {trend.labels.length > 0
                      ? `日粒度 · ${trend.labels.length} 点 · 合计 ${formatMicroUsd(trend.totalMicro)}`
                      : 'metric_daily 无数据'}
                  </span>
                </div>
                {trend.labels.length > 0 ? (
                  <div className="fm-chart">
                    <EchartsBase option={trendOption} height="100%" />
                  </div>
                ) : (
                  <div className="fm-empty">
                    {overview.isPending ? '正在加载…' : '所选区间没有 cost 日聚合（metric_daily 为空）。'}
                  </div>
                )}
              </section>

              {/* ── 右栏：实时运行流 ── */}
              <section className="panel-card panel-card--neon fm-panel fm-feed">
                <div className="fm-panel__head">
                  <span className="fm-panel__title">实时运行流</span>
                  <span className="fm-panel__hint">
                    {`最近 ${runs.length} 条 · ${activeModuleLabel} · 10s 轮询`}
                  </span>
                </div>
                {feedRuns.length === 0 ? (
                  <div className="fm-empty">
                    {runsQuery.isPending ? '正在加载…' : '暂无运行记录（去对话页提问后这里会有记录）。'}
                  </div>
                ) : (
                  <div className="fm-feed__list">
                    {feedRuns.map((run) => {
                      const meta = runStatusMeta(run.status);
                      return (
                        <div
                          className="fm-feed__row"
                          key={`${run.trace_id}-${run.started_at ?? ''}`}
                          data-tone={meta.tone}
                        >
                          <span className="fm-feed__time">{clockOf(run.started_at)}</span>
                          <span className="fm-tag" data-tone={meta.tone}>
                            {meta.label}
                          </span>
                          <span className="fm-feed__mod" title={run.module || '—'}>
                            {moduleLabel(run.module, true)}
                          </span>
                          <span className="fm-feed__trace" title={run.trace_id || '无 trace_id'}>
                            {run.trace_id ? (
                              <Link to={`/observability/runs/${encodeURIComponent(run.trace_id)}`}>
                                {run.trace_id.slice(0, 8)}
                              </Link>
                            ) : (
                              '—'
                            )}
                          </span>
                          <span className="fm-feed__lat">{formatMs(run.latency_ms)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
