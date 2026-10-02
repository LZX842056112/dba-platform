import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RecommendationsPage } from './RecommendationsPage';

const mocks = vi.hoisted(() => ({
  getRecommendations: vi.fn(),
  getLoopAlerts: vi.fn(),
  applyRecommendation: vi.fn(),
  rejectRecommendation: vi.fn(),
}));

vi.mock('../../features/finops/api', () => ({
  getRecommendations: mocks.getRecommendations,
  getLoopAlerts: mocks.getLoopAlerts,
  applyRecommendation: mocks.applyRecommendation,
  rejectRecommendation: mocks.rejectRecommendation,
}));

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecommendationsPage />
    </QueryClientProvider>,
  );
}

describe('RecommendationsPage', () => {
  beforeEach(() => {
    mocks.getRecommendations.mockReset();
    mocks.getLoopAlerts.mockReset();
    mocks.applyRecommendation.mockReset();
    mocks.rejectRecommendation.mockReset();
    mocks.getRecommendations.mockResolvedValue([
      { reco_id: 'reco-synthetic-42', type: 'cache', status: 'pending', reason: 'test' },
    ]);
    mocks.getLoopAlerts.mockResolvedValue([]);
    mocks.rejectRecommendation.mockResolvedValue({ ok: true });
    mocks.applyRecommendation.mockResolvedValue({ impact: 'dry_run' });
  });

  it('uses reco_id for dry-run and reject actions and displays persisted status', async () => {
    renderPage();
    await screen.findByText('reco-synthetic-42');

    fireEvent.click(screen.getByRole('button', { name: '试算' }));
    await waitFor(() =>
      expect(mocks.applyRecommendation).toHaveBeenCalledWith('reco-synthetic-42', true),
    );
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }));
    await waitFor(() => expect(mocks.rejectRecommendation).toHaveBeenCalledWith('reco-synthetic-42'));
    expect(screen.getByText('pending')).toBeTruthy();
  });

  it('shows mutation failures to the operator', async () => {
    mocks.rejectRecommendation.mockRejectedValueOnce(new Error('reject failed'));
    renderPage();
    await screen.findByText('reco-synthetic-42');
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }));
    expect((await screen.findByRole('alert')).textContent).toContain('reject failed');
  });
});
