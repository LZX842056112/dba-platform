// 预算 + 护栏策略（admin 可改）。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Empty, PagePanel, SimpleList } from '../../components/PageBits';
import { getBudgets, getGuardrailPolicy, updateGuardrailPolicy } from '../../features/finops/api';

const BOOL_KEYS: Array<keyof NonNullable<Awaited<ReturnType<typeof getGuardrailPolicy>>>> = [
  'enabled',
  'allow_downgrade',
  'allow_compress',
  'allow_rate_limit',
  'allow_circuit_break',
  'kill_switch',
];

export function BudgetPage() {
  const qc = useQueryClient();
  const { data: budgets } = useQuery({ queryKey: ['finops-budgets'], queryFn: getBudgets, refetchInterval: 30000 });
  const { data: policy } = useQuery({ queryKey: ['finops-policy'], queryFn: getGuardrailPolicy });

  const update = useMutation({
    mutationFn: updateGuardrailPolicy,
    onSettled: () => qc.invalidateQueries({ queryKey: ['finops-policy'] }),
  });

  const list = (budgets ?? []) as Array<Record<string, unknown>>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <PagePanel title="预算列表">
        {list.length ? <SimpleList items={list} /> : <Empty text="暂无预算（后端 /budgets 当前为占位）" />}
      </PagePanel>
      <PagePanel title="护栏策略（进程内，重启失效）">
        {policy ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
            {BOOL_KEYS.map((k) => (
              <label key={k} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
                <input
                  type="checkbox"
                  checked={Boolean(policy[k])}
                  onChange={(e) => update.mutate({ [k]: e.target.checked })}
                />
                {k}
              </label>
            ))}
          </div>
        ) : (
          <Empty text="暂无策略" />
        )}
      </PagePanel>
    </div>
  );
}
