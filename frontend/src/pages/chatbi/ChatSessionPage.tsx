// 对话页（最小主链路）：输入 → SSE 流 → 进度/自愈提示 → 逐面板出图。
// 对齐《设计方案 v2》§9.1 页面落位与 §9.3 事件渲染。

import { CockpitFrame } from '../../features/dashboard/components/CockpitFrame';
import { DashboardGrid } from '../../features/dashboard/components/DashboardGrid';
import { NarrationPanel } from '../../features/chat/components/NarrationPanel';
import { QueryBar } from '../../features/chat/components/QueryBar';
import { RetryHint } from '../../features/chat/components/RetryHint';
import { RunBanner } from '../../features/chat/components/RunBanner';
import { SqlPanel } from '../../features/chat/components/SqlPanel';
import { StepProgress } from '../../features/chat/components/StepProgress';
import { useChatRun } from '../../features/chat/hooks/useChatRun';
import { useDashboardHydration } from '../../features/chat/hooks/useDashboardHydration';
import { useRunStore } from '../../features/chat/store/runStore';

export function ChatSessionPage() {
  const steps = useRunStore((state) => state.steps);
  const sql = useRunStore((state) => state.sql);
  const retrying = useRunStore((state) => state.retrying);
  const narration = useRunStore((state) => state.narration);
  const usage = useRunStore((state) => state.usage);
  const error = useRunStore((state) => state.error);
  const banner = useRunStore((state) => state.banner);
  const status = useRunStore((state) => state.status);
  const layout = useRunStore((state) => state.layout);
  const dataSources = useRunStore((state) => state.dataSources);
  const panelOrder = useRunStore((state) => state.panelOrder);
  const panels = useRunStore((state) => state.panels);

  const { start, busy } = useChatRun();
  useDashboardHydration();

  const ordered = panelOrder
    .map((id) => panels[id])
    .filter((panel): panel is NonNullable<typeof panel> => Boolean(panel));

  return (
    <div className="chat-session">
      <section className="panel">
        <div className="panel-title">对话</div>
        <QueryBar onSubmit={start} disabled={busy} />
        <RunBanner banner={banner} error={error} status={status} />
        <StepProgress steps={steps} />
        <RetryHint retrying={retrying} />
        <SqlPanel sql={sql} />
        <NarrationPanel text={narration} usage={usage} />
      </section>

      <section className="panel">
        {/* ★ 此 `.panel-title` 是浏览器 e2e 断言（≥2）的依赖，勿移除 */}
        <div className="panel-title">数据大屏</div>
        <CockpitFrame title="经营数据驾驶舱" count={ordered.length}>
          <DashboardGrid panels={ordered} layout={layout} dataSources={dataSources} />
        </CockpitFrame>
      </section>
    </div>
  );
}
