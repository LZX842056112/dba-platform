// Run 详情：span 列表（自愈痕迹可见于重试/回退标记）。

import { useQuery } from '@tanstack/react-query';
import { useParams } from 'react-router-dom';
import { Empty, PagePanel, SimpleList } from '../../components/PageBits';
import { getRunDetail } from '../../features/observability/api';

export function RunDetailPage() {
  const { traceId } = useParams();
  const { data } = useQuery({
    queryKey: ['obs-run', traceId],
    queryFn: () => getRunDetail(traceId as string),
    enabled: Boolean(traceId),
  });
  const spans = (data?.spans ?? []) as Array<Record<string, unknown>>;

  return (
    <PagePanel title={`Run 详情 · ${traceId ?? ''}`}>
      {spans.length > 0 ? (
        <SimpleList items={spans} emptyText="暂无 span" />
      ) : (
        <Empty text="暂无 span 数据（该 Run 的 span 尚未落 Mongo 或已过期）" />
      )}
    </PagePanel>
  );
}
