import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OverviewPage } from './OverviewPage';

vi.mock('../../features/observability/api', () => ({
  getOverview: vi.fn(),
  getSelfCost: vi.fn(),
}));

import { getOverview, getSelfCost } from '../../features/observability/api';

describe('OverviewPage KPI response shape', () => {
  beforeEach(() => {
    vi.mocked(getOverview).mockResolvedValue({
      kpi_cards: {
        running_now: { value: 12, label: '当前运行中 Run' },
        cost_today_micro: { value: 1234 },
        success_rate: { value: 0.875 },
        silent_failures: { value: 3, label: '静默失败（窗口内）' },
      },
      trend: { granularity: 'hour', series: [] },
      top_agents: [],
    });
    vi.mocked(getSelfCost).mockResolvedValue({ by_module: {}, total_cost_micro_usd: 0, total_runs: 0 });
  });

  it('renders values wrapped in the backend value/label object', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <OverviewPage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText('12')).toBeTruthy();
    expect(screen.getByText('0.875')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
  });
});
