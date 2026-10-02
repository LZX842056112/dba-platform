import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StepProgress } from './StepProgress';

describe('StepProgress terminal states', () => {
  it('shows a failure badge for a step that did not finish', () => {
    render(<StepProgress steps={{ sql_guard: { status: 'error', attempt: 2 } }} />);

    expect(screen.getByText('失败')).toBeTruthy();
    expect(screen.getByText('SQL 校验')).toBeTruthy();
  });
});
