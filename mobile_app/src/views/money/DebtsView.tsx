import { useRef, useState } from 'react';
import { Button, EmptyState, ErrorNote, Segmented, SkeletonList, riseStyle } from '../../components/ui';
import { formatMoney } from '../../lib/apps';
import { useTopic } from '../../lib/bus';
import { useHubData } from '../../lib/hub';
import type { Debt, DebtDirection } from '../../lib/moneyTypes';
import { usePullRefresh } from '../../lib/nav';
import { DebtRow, DebtSheet, OwedTotals } from './DebtSheets';

type Filter = 'open' | 'settled' | 'all';

/** Every loan: open ones first, settled history on demand, net per person. */
export default function DebtsView() {
  const [filter, setFilter] = useState<Filter>('open');
  const [sheet, setSheet] = useState<{ id: number; open: boolean; debt: Debt | null; direction?: DebtDirection } | null>(null);
  const next = useRef(1);
  const data = useHubData((c, s) => c.debts(filter, s), [filter]);
  useTopic(['money'], data.reload);
  usePullRefresh(data.reload);
  const open = (debt: Debt | null, direction?: DebtDirection) => setSheet({ id: next.current++, open: true, debt, direction });
  const close = () => setSheet((s) => (s ? { ...s, open: false } : s));
  const list = data.data?.debts ?? [];

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-2">
        <Button variant="primary" icon="up" onClick={() => open(null, 'lent')}>
          I lent
        </Button>
        <Button icon="down" onClick={() => open(null, 'borrowed')}>
          I borrowed
        </Button>
      </div>
      <p className="px-1 text-[13px] text-ink-400">Or say it: “j’ai prêté 200 dh à Ayoub”, “Ayoub m’a rendu 100”.</p>

      {data.data && <OwedTotals data={data.data} />}

      {data.data && data.data.summary.by_person.length > 1 && (
        <section className="rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
          <h2 className="px-1 pb-1 text-[13px] text-ink-400">By person</h2>
          <ul className="space-y-1">
            {data.data.summary.by_person.map((p) => (
              <li key={p.person} className="flex items-baseline justify-between gap-2 px-1 text-[14px]">
                <span className="truncate text-ink-200">{p.person}</span>
                <span className={p.net >= 0 ? 'tabular-nums text-signal' : 'tabular-nums text-danger-400'}>
                  {p.net >= 0 ? '+' : '-'}
                  {formatMoney(Math.abs(p.net), data.data!.summary.currency)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'open', label: 'Open' },
          { value: 'settled', label: 'Settled' },
          { value: 'all', label: 'All' },
        ]}
      />

      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
      {data.loading && !data.data && <SkeletonList rows={3} />}
      {data.data && !list.length && (
        <EmptyState icon="cash" title={filter === 'settled' ? 'Nothing settled yet' : 'Nobody owes anything'} hint="Loans to and from friends, with partial repayments and a reminder on the due date." />
      )}
      {list.length > 0 && (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {list.map((d, i) => (
            <div key={d.id} style={riseStyle(i)}>
              <DebtRow debt={d} i={i} onClick={() => open(d)} />
            </div>
          ))}
        </div>
      )}

      {sheet && <DebtSheet key={sheet.id} open={sheet.open} debt={sheet.debt} direction={sheet.direction} onClose={close} />}
    </div>
  );
}
