import { render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RouteErrorBoundary } from './RouteErrorBoundary';

function BrokenPage(): ReactElement {
  throw new Error('页面数据异常');
}

describe('RouteErrorBoundary', () => {
  afterEach(() => vi.restoreAllMocks());

  it('catches a page error while content outside the boundary remains mounted', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const suppressWindowError = (event: ErrorEvent) => event.preventDefault();
    window.addEventListener('error', suppressWindowError);
    try {
      render(
        <>
          <nav>全局导航</nav>
          <RouteErrorBoundary>
            <BrokenPage />
          </RouteErrorBoundary>
        </>,
      );
    } finally {
      window.removeEventListener('error', suppressWindowError);
    }

    expect(screen.getByText('全局导航')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('页面数据异常');
    expect(screen.getByRole('heading', { name: '页面加载失败' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });
});
