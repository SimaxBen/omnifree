import { useEffect, useMemo, useState } from 'react';
import { Icon } from '../../components/icons';
import { SwipeRow } from '../../components/SwipeRow';
import { Button, Chip, EmptyState, ErrorNote, IconButton, SkeletonList, cx, riseStyle } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { categoryBreakdown, currenciesByWeight, currentMonth, formatMoney, monthLabel, shiftMonth, type CategorySlice } from '../../lib/apps';
import { announce, useTopic } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { usePullRefresh } from '../../lib/nav';
import type { Expense, ExpenseCategory } from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';

/** Donut of category shares; segments sweep in when the data changes. */
function Donut({ slices, total, currency }: { slices: CategorySlice[]; total: number; currency: string }) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const [shown, setShown] = useState(false);
  useEffect(() => {
    setShown(false);
    const t = window.requestAnimationFrame(() => setShown(true));
    return () => window.cancelAnimationFrame(t);
  }, [slices]);
  let offset = 0;
  return (
    <svg viewBox="0 0 140 140" className="h-40 w-40 shrink-0" role="img" aria-label={`Spending by category: ${slices.map((s) => `${s.category} ${Math.round(s.share * 100)}%`).join(', ')}`}>
      <circle cx="70" cy="70" r={r} fill="none" stroke="#1C2146" strokeWidth="16" />
      <g transform="rotate(-90 70 70)">
        {slices.map((s) => {
          const len = Math.max(s.share * c - 2, 0.5);
          const el = (
            <circle
              key={s.category}
              cx="70"
              cy="70"
              r={r}
              fill="none"
              stroke={CATEGORY_COLOR[s.category as ExpenseCategory] ?? '#8A8FBF'}
              strokeWidth="16"
              strokeDasharray={`${shown ? len : 0} ${c}`}
              strokeDashoffset={-offset}
              style={{ transition: 'stroke-dasharray 600ms cubic-bezier(0.22, 1, 0.36, 1)' }}
            />
          );
          offset += s.share * c;
          return el;
        })}
      </g>
      <text x="70" y="66" textAnchor="middle" className="fill-ink-400" style={{ font: '500 10px "Atkinson Hyperlegible Next", sans-serif' }}>
        {currency}
      </text>
      <text x="70" y="84" textAnchor="middle" className="fill-ink-100" style={{ font: '700 17px "Bricolage Grotesque", sans-serif' }}>
        {total >= 10000 ? `${(total / 1000).toFixed(1)}k` : Math.round(total).toLocaleString('en-GB')}
      </text>
    </svg>
  );
}

function dayLabel(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
}

export default function ExpensesView() {
  const { client, notify } = useHub();
  const actions = useActions();
  const [month, setMonth] = useState(currentMonth());
  const [currency, setCurrency] = useState<string | null>(null);
  const data = useHubData((c, s) => c.expenses(month, s), [month]);
  useTopic(['expenses'], data.reload);
  usePullRefresh(data.reload);

  const totals = data.data?.summary.total_by_currency ?? {};
  const currencies = currenciesByWeight(totals);
  const cur = currency && totals[currency] !== undefined ? currency : currencies[0];
  const slices = useMemo(() => (cur ? categoryBreakdown(data.data?.summary.by_category ?? {}, cur) : []), [data.data, cur]);
  const byDay = useMemo(() => {
    const map = new Map<string, Expense[]>();
    for (const e of [...(data.data?.expenses ?? [])].sort((a, b) => b.date.localeCompare(a.date) || b.created_at.localeCompare(a.created_at))) {
      map.set(e.date, [...(map.get(e.date) ?? []), e]);
    }
    return [...map.entries()];
  }, [data.data]);

  const remove = async (e: Expense) => {
    try {
      await client.deleteExpense(e.id);
      data.setData((d) => d && { ...d, expenses: d.expenses.filter((x) => x.id !== e.id) });
      announce('expenses');
      notify(`Deleted ${e.merchant || 'expense'}`, 'ok');
      data.reload();
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const isNow = month === currentMonth();
  let n = 0;
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-2">
        <Button variant="primary" icon="camera" onClick={actions.scanReceipt}>
          Scan receipt
        </Button>
        <Button icon="plus" onClick={actions.addExpense}>
          Add by hand
        </Button>
      </div>

      <div className="flex items-center justify-between rounded-full bg-ink-900 p-1 ring-1 ring-inset ring-ink-800">
        <IconButton icon="left" label="Previous month" onClick={() => setMonth((m) => shiftMonth(m, -1))} />
        <p key={month} className="screen-in-up font-display text-[17px] font-semibold text-ink-100">
          {monthLabel(month)}
        </p>
        <IconButton icon="right" label="Next month" onClick={() => setMonth((m) => shiftMonth(m, 1))} disabled={isNow} />
      </div>

      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}

      {data.loading && !data.data ? (
        <div className="skeleton h-48 rounded-card" />
      ) : (
        cur && (
          <section className="rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
            {currencies.length > 1 && (
              <div className="no-scrollbar -mx-1 mb-3 flex gap-2 overflow-x-auto px-1">
                {currencies.map((c) => (
                  <Chip key={c} active={c === cur} onClick={() => setCurrency(c)}>
                    {formatMoney(totals[c], c)}
                  </Chip>
                ))}
              </div>
            )}
            <div className="flex items-center gap-4">
              <Donut slices={slices} total={totals[cur] ?? 0} currency={cur} />
              <ul className="min-w-0 flex-1 space-y-2">
                {slices.slice(0, 6).map((s) => (
                  <li key={s.category} className="text-[13px]">
                    <span className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-1.5 text-ink-200">
                        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CATEGORY_COLOR[s.category as ExpenseCategory] ?? '#8A8FBF' }} />
                        <span className="truncate capitalize">{s.category}</span>
                      </span>
                      <span className="shrink-0 text-ink-400">{Math.round(s.share * 100)}%</span>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <p className="pt-3 text-[13px] text-ink-400">
              {data.data?.summary.count ?? 0} expense{data.data?.summary.count === 1 ? '' : 's'} in {monthLabel(month).split(' ')[0]}
            </p>
          </section>
        )
      )}

      {data.loading && !data.data && <SkeletonList rows={4} />}
      {data.data && !data.data.expenses.length && (
        <EmptyState icon="receipt" title={`Nothing spent in ${monthLabel(month).split(' ')[0]}`} hint="Snap a receipt: the merchant, total and category are filled in for you." />
      )}

      {byDay.map(([day, items]) => (
        <section key={day} className="space-y-2">
          <h2 className="flex justify-between px-1 text-[14px]">
            <span className="font-display font-semibold text-ink-300">{dayLabel(day)}</span>
          </h2>
          <div className="space-y-2">
            {items.map((e) => (
              <div key={e.id} className="rise" style={riseStyle(n++)}>
                <SwipeRow end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => remove(e) }}>
                  <button type="button" onClick={() => actions.editExpense(e)} className="flex min-h-[64px] w-full items-center gap-3 px-3 py-2 text-left">
                    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full" style={{ background: `${CATEGORY_COLOR[e.category] ?? '#8A8FBF'}22`, color: CATEGORY_COLOR[e.category] ?? '#8A8FBF' }}>
                      <Icon name={e.receipt_key ? 'receipt' : 'wallet'} size={18} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[15px] text-ink-100">{e.merchant || 'Untitled'}</span>
                      <span className="block truncate text-[12px] capitalize text-ink-500">
                        {e.category}
                        {e.note ? `, ${e.note}` : ''}
                      </span>
                    </span>
                    <span className={cx('shrink-0 font-display text-[16px] font-semibold', 'text-ink-100')}>{formatMoney(e.total, e.currency)}</span>
                  </button>
                </SwipeRow>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
