import { useMemo, useRef, useState } from 'react';
import { Icon, type IconName } from '../../components/icons';
import { Badge, Button, ErrorNote, IconButton, Meter, SectionTitle, cx, riseStyle, toneForFraction } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { currentMonth, formatMoney, monthLabel, shiftMonth } from '../../lib/apps';
import { useTopic } from '../../lib/bus';
import { useHubData } from '../../lib/hub';
import { cashflowBars, compactAmount, fxLabel, monthInitial, monthRangeLabel, savingsLabel } from '../../lib/money';
import { useNav, usePullRefresh } from '../../lib/nav';
import type { ExpenseCategory, MoneyAccount, MoneyOverview, RecurringExpense } from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';
import { BankSmsSheet } from './BankSmsSheet';
import { MoneyToolTiles, OwedSection } from './MoneyExtras';
import { AccountSheet, BudgetSheet, INCOME_LABEL, RecurringSheet } from './MoneySheets';
import { ForecastLine, ForecastSheet, GoalsSection, SubscriptionsSection } from './MoneyIntel';

type LocalSheet =
  | { kind: 'recurring'; item: RecurringExpense | null }
  | { kind: 'budget'; category: ExpenseCategory | null; current: { amount: number; currency: string } | null }
  | { kind: 'account'; account: MoneyAccount | null }
  | { kind: 'sms' }
  | { kind: 'forecast' };

const MAD = 'MAD';

function dayShort(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

function Original({ amount, currency }: { amount: number; currency: string }) {
  if (currency === MAD) return null;
  return <span className="block text-[12px] text-ink-500">{formatMoney(amount, currency)}</span>;
}

function Hero({ ov, onForecast }: { ov: MoneyOverview; onForecast: () => void }) {
  const rate = ov.savings_rate;
  const negative = ov.net < 0;
  return (
    <section className="rise relative overflow-hidden rounded-card bg-ink-900 p-5 ring-1 ring-inset ring-ink-800">
      <div aria-hidden="true" className={cx('pointer-events-none absolute -right-16 -top-16 h-48 w-48 rounded-full blur-3xl', negative ? 'bg-danger-400/10' : 'bg-signal/10')} />
      <p className="text-[13px] text-ink-400">Left this month</p>
      <p key={ov.month} className={cx('screen-in-up pt-1 font-display text-[40px] font-bold leading-none tabular-nums', negative ? 'text-danger-400' : 'text-ink-100')}>
        {Math.round(ov.net).toLocaleString('en-GB')}
        <span className="pl-2 text-[17px] font-semibold text-ink-400">MAD</span>
      </p>
      <div className="grid grid-cols-3 gap-2 pt-5">
        <div>
          <p className="flex items-center gap-1.5 text-[12px] text-ink-400">
            <span className="h-2 w-2 rounded-full bg-mint-400" />
            Income
          </p>
          <p className="pt-0.5 font-display text-[18px] font-semibold tabular-nums text-ink-100">{compactAmount(ov.income.total)}</p>
        </div>
        <div>
          <p className="flex items-center gap-1.5 text-[12px] text-ink-400">
            <span className="h-2 w-2 rounded-full bg-amber-400" />
            Spent
          </p>
          <p className="pt-0.5 font-display text-[18px] font-semibold tabular-nums text-ink-100">{compactAmount(ov.spent.total)}</p>
        </div>
        <div>
          <p className="text-[12px] text-ink-400">Saved</p>
          <p className={cx('pt-0.5 font-display text-[18px] font-semibold tabular-nums', rate === null ? 'text-ink-500' : rate < 0 ? 'text-danger-400' : 'text-signal')}>{savingsLabel(rate)}</p>
        </div>
      </div>
      {ov.income.total > 0 && (
        <div className="mt-4 h-2 overflow-hidden rounded-full bg-ink-800" role="img" aria-label={`Spent ${Math.round((ov.spent.total / ov.income.total) * 100)}% of income`}>
          <div
            className={cx('h-full rounded-full transition-[width] duration-slow ease-out', ov.spent.total > ov.income.total ? 'bg-danger-400' : 'bg-amber-400')}
            style={{ width: `${Math.min(100, Math.max(2, (ov.spent.total / ov.income.total) * 100))}%` }}
          />
        </div>
      )}
      <p className="pt-3 text-[12px] text-ink-500">
        {ov.spent.count} expense{ov.spent.count === 1 ? '' : 's'}
        {ov.spent.recurring > 0 && ` + ${formatMoney(ov.spent.recurring, MAD)} recurring`}. Totals in MAD.
      </p>
      {ov.forecast && ov.forecast.remaining_days > 0 && <ForecastLine forecast={ov.forecast} onOpen={onForecast} />}
    </section>
  );
}

function CashFlow({ ov }: { ov: MoneyOverview }) {
  const H = 96;
  const bars = cashflowBars(ov.series, H);
  return (
    <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(3)}>
      <div className="flex items-center justify-between pb-3">
        <h2 className="font-display text-[17px] font-semibold text-ink-100">6 months</h2>
        <span className="flex gap-3 text-[12px] text-ink-400">
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-mint-400" />
            In
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-amber-400" />
            Out
          </span>
        </span>
      </div>
      <div className="flex items-end justify-between gap-2" style={{ height: H + 22 }} role="img" aria-label={ov.series.map((s) => `${monthLabel(s.month)}: in ${Math.round(s.income)}, out ${Math.round(s.spent)} MAD`).join('; ')}>
        {ov.series.map((s, i) => {
          const isCurrent = s.month === ov.month;
          return (
            <div key={s.month} className="flex min-w-0 flex-1 flex-col items-center gap-1.5">
              <div className="flex items-end gap-1" style={{ height: H }}>
                <span className="w-2.5 rounded-t-[4px] bg-mint-400 transition-[height] duration-slow ease-out sm:w-3.5" style={{ height: bars[i].income, opacity: isCurrent ? 1 : 0.55 }} />
                <span className="w-2.5 rounded-t-[4px] bg-amber-400 transition-[height] duration-slow ease-out sm:w-3.5" style={{ height: bars[i].spent, opacity: isCurrent ? 1 : 0.55 }} />
              </div>
              <span className={cx('text-[11px]', isCurrent ? 'font-semibold text-ink-100' : 'text-ink-500')}>{monthInitial(s.month)}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Row({
  icon,
  tint,
  title,
  detail,
  amount,
  original,
  tone,
  onClick,
  i,
}: {
  icon: IconName;
  tint: string;
  title: string;
  detail: string;
  amount: string;
  original?: { amount: number; currency: string };
  tone?: string;
  onClick: () => void;
  i: number;
}) {
  return (
    <button type="button" onClick={onClick} className="press rise flex min-h-[60px] w-full items-center gap-3 px-4 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full" style={{ background: `${tint}22`, color: tint }}>
        <Icon name={icon} size={18} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[15px] text-ink-100">{title}</span>
        <span className="block truncate text-[12px] text-ink-500">{detail}</span>
      </span>
      <span className="shrink-0 text-right">
        <span className={cx('block font-display text-[16px] font-semibold tabular-nums', tone ?? 'text-ink-100')}>{amount}</span>
        {original && <Original amount={original.amount} currency={original.currency} />}
      </span>
    </button>
  );
}

export default function MoneyView() {
  const actions = useActions();
  const { go } = useNav();
  const [month, setMonth] = useState(currentMonth());
  const [sheet, setSheet] = useState<(LocalSheet & { id: number; open: boolean }) | null>(null);
  const nextId = useRef(1);
  const data = useHubData((c, s) => c.moneyOverview(month, s), [month]);
  useTopic(['money', 'expenses'], data.reload);
  usePullRefresh(data.reload);

  const open = (s: LocalSheet) => setSheet({ ...s, id: nextId.current++, open: true });
  const close = () => setSheet((s) => (s ? { ...s, open: false } : s));
  const ov = data.data;
  const isNow = month === currentMonth();
  const budgets = useMemo(() => (ov ? ov.categories.filter((c) => c.budget !== null || c.spent > 0) : []), [ov]);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between rounded-full bg-ink-900 p-1 ring-1 ring-inset ring-ink-800">
        <IconButton icon="left" label="Previous month" onClick={() => setMonth((m) => shiftMonth(m, -1))} />
        <button type="button" onClick={() => setMonth(currentMonth())} className="press min-h-[40px] px-2" aria-label="Back to this month">
          <span key={month} className="screen-in-up block font-display text-[17px] font-semibold text-ink-100">
            {monthLabel(month)}
          </span>
        </button>
        <IconButton icon="right" label="Next month" onClick={() => setMonth((m) => shiftMonth(m, 1))} disabled={month >= shiftMonth(currentMonth(), 1)} />
      </div>

      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}

      {!ov ? (
        data.loading && (
          <div className="space-y-3" aria-hidden="true">
            <div className="skeleton h-48 rounded-card" />
            <div className="skeleton h-14 rounded-full" />
            <div className="skeleton h-40 rounded-card" />
          </div>
        )
      ) : (
        <>
          <Hero ov={ov} onForecast={() => open({ kind: 'forecast' })} />

          <div className="rise grid grid-cols-[1fr_auto] gap-2" style={riseStyle(1)}>
            <Button variant="primary" icon="mic" onClick={actions.recordMoney}>
              Say it
            </Button>
            <Button icon="plus" onClick={() => actions.editIncome(null)}>
              Income
            </Button>
          </div>

          {/* Accounts: the money I have */}
          <section className="rise" style={riseStyle(2)}>
            <SectionTitle action={<IconButton icon="plus" label="Add account" onClick={() => open({ kind: 'account', account: null })} />}>
              Accounts
              {ov.accounts.items.length > 0 && <span className="pl-2 font-display text-[15px] font-semibold text-signal">{formatMoney(ov.accounts.total, MAD)}</span>}
            </SectionTitle>
            {ov.accounts.items.length ? (
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                {ov.accounts.items.map((a, i) => (
                  <Row
                    key={a.id}
                    i={i}
                    icon={a.kind === 'cash' ? 'cash' : 'wallet'}
                    tint="#A9CBFF"
                    title={a.name}
                    detail={`Updated ${dayShort(a.updated_at.slice(0, 10))}`}
                    amount={formatMoney(a.currency === MAD ? a.balance : a.balance_mad, MAD)}
                    original={{ amount: a.balance, currency: a.currency }}
                    tone={a.balance < 0 ? 'text-danger-400' : undefined}
                    onClick={() => open({ kind: 'account', account: a })}
                  />
                ))}
              </div>
            ) : (
              <button
                type="button"
                onClick={() => open({ kind: 'account', account: null })}
                className="press w-full rounded-card border border-dashed border-ink-700 px-4 py-4 text-left text-[14px] text-ink-400"
              >
                Add your cash and bank accounts to see the money you have.
              </button>
            )}
          </section>

          {/* Budgets */}
          <section className="rise" style={riseStyle(3)}>
            <SectionTitle action={<IconButton icon="plus" label="Set a budget" onClick={() => open({ kind: 'budget', category: null, current: null })} />}>Budgets</SectionTitle>
            {budgets.length ? (
              <div className="space-y-1 rounded-card bg-ink-900 p-2 ring-1 ring-inset ring-ink-800">
                {budgets.map((c, i) => {
                  const cat = c.category as ExpenseCategory;
                  return (
                    <button
                      key={c.category}
                      type="button"
                      onClick={() => open({ kind: 'budget', category: cat, current: c.budget_original })}
                      className="press rise block w-full rounded-row px-2 py-2.5 text-left hover:bg-ink-850"
                      style={riseStyle(i)}
                    >
                      <span className="flex items-baseline justify-between gap-2 text-[14px]">
                        <span className="flex min-w-0 items-center gap-1.5 capitalize text-ink-200">
                          <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CATEGORY_COLOR[cat] ?? '#8A8FBF' }} />
                          <span className="truncate">{c.category}</span>
                        </span>
                        <span className="shrink-0 tabular-nums text-ink-100">
                          {compactAmount(c.spent)}
                          <span className="text-ink-500">{c.budget !== null ? ` / ${compactAmount(c.budget)}` : ' · no budget'}</span>
                        </span>
                      </span>
                      {c.budget !== null && <Meter value={c.fraction} tone={toneForFraction(c.fraction)} className="mt-2" />}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="px-1 text-[14px] text-ink-400">Nothing spent yet this month.</p>
            )}
          </section>

          <CashFlow ov={ov} />

          {ov.upcoming.length > 0 && (
            <section className="rise" style={riseStyle(4)}>
              <SectionTitle>Coming up</SectionTitle>
              <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4">
                {ov.upcoming.map((u) => (
                  <div key={`${u.kind}-${u.id}`} className="min-w-[140px] rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
                    <p className="text-[12px] text-ink-500">{dayShort(u.date)}</p>
                    <p className="truncate pt-1 text-[14px] text-ink-200">{u.name}</p>
                    <p className={cx('font-display text-[16px] font-semibold tabular-nums', u.kind === 'income' ? 'text-mint-400' : 'text-ink-100')}>
                      {u.kind === 'income' ? '+' : '-'}
                      {formatMoney(u.amount, u.currency)}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* Recurring expenses */}
          <section className="rise" style={riseStyle(5)}>
            <SectionTitle action={<IconButton icon="plus" label="Add a recurring expense" onClick={() => open({ kind: 'recurring', item: null })} />}>
              Every month
              {ov.spent.recurring > 0 && <span className="pl-2 text-[14px] font-normal text-ink-400">{formatMoney(ov.spent.recurring, MAD)}</span>}
            </SectionTitle>
            {ov.recurring.length ? (
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                {ov.recurring.map((r, i) => (
                  <Row
                    key={r.id}
                    i={i}
                    icon="repeat"
                    tint={CATEGORY_COLOR[r.category] ?? '#8A8FBF'}
                    title={r.name}
                    detail={`${r.upcoming ? 'Due' : 'Paid'} ${dayShort(r.on)}${r.paid ? ' · counted in expenses' : ''}${r.end_month ? ` · ${monthRangeLabel(r.start_month, r.end_month)}` : ''}`}
                    amount={formatMoney(r.amount_mad, MAD)}
                    original={{ amount: r.amount, currency: r.currency }}
                    onClick={() => open({ kind: 'recurring', item: r })}
                  />
                ))}
              </div>
            ) : (
              <button
                type="button"
                onClick={() => open({ kind: 'recurring', item: null })}
                className="press w-full rounded-card border border-dashed border-ink-700 px-4 py-4 text-left text-[14px] text-ink-400"
              >
                Rent, internet, subscriptions: add them once, they count every month.
              </button>
            )}
          </section>

          {isNow && <SubscriptionsSection i={5} />}

          <GoalsSection accounts={ov.accounts.items} i={6} />

          {/* Incomes */}
          <section className="rise" style={riseStyle(6)}>
            <SectionTitle action={<IconButton icon="plus" label="Add income" onClick={() => actions.editIncome(null)} />}>Income</SectionTitle>
            {ov.income.items.length ? (
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                {ov.income.items.map((inc, i) => (
                  <Row
                    key={inc.id}
                    i={i}
                    icon={inc.recurring ? 'repeat' : 'cash'}
                    tint="#5EE0A8"
                    title={inc.source || INCOME_LABEL[inc.category]}
                    detail={`${INCOME_LABEL[inc.category]} · ${dayShort(inc.on)}${inc.recurring ? ' · monthly' : ''}${inc.origin === 'voice' ? ' · voice' : ''}`}
                    amount={`+${formatMoney(inc.amount_mad, MAD)}`}
                    original={{ amount: inc.amount, currency: inc.currency }}
                    tone="text-mint-400"
                    onClick={() => actions.editIncome(inc)}
                  />
                ))}
              </div>
            ) : (
              <button type="button" onClick={() => actions.editIncome(null)} className="press w-full rounded-card border border-dashed border-ink-700 px-4 py-4 text-left text-[14px] text-ink-400">
                Add your salary once as a monthly income: every month counts it.
              </button>
            )}
          </section>

          <OwedSection i={7} />

          <MoneyToolTiles i={8} />

          <button
            type="button"
            onClick={() => go('more', 'expenses')}
            className="press rise flex min-h-[60px] w-full items-center gap-3 rounded-card bg-ink-900 px-4 py-2 text-left ring-1 ring-inset ring-ink-800"
            style={riseStyle(7)}
          >
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-signal/15 text-signal">
              <Icon name="receipt" size={20} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-[15px] text-ink-100">Expenses</span>
              <span className="block truncate text-[13px] text-ink-400">
                {ov.spent.count} this month, {formatMoney(ov.spent.expenses, MAD)}: receipts, email, SMS and voice
              </span>
            </span>
            <Icon name="right" size={18} className="text-ink-500" />
          </button>

          <button
            type="button"
            onClick={() => open({ kind: 'sms' })}
            className="press rise flex min-h-[60px] w-full items-center gap-3 rounded-card bg-ink-900 px-4 py-2 text-left ring-1 ring-inset ring-ink-800"
            style={riseStyle(8)}
          >
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-[#7FA8FF]/15 text-[#A9CBFF]">
              <Icon name="chat" size={20} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-[15px] text-ink-100">Bank SMS</span>
              <span className="block truncate text-[13px] text-ink-400">Card payments and transfers from your bank's alerts</span>
            </span>
            <Icon name="right" size={18} className="text-ink-500" />
          </button>

          <p className="flex flex-wrap items-center justify-center gap-2 px-2 pb-2 text-center text-[12px] text-ink-500">
            {fxLabel(ov.fx.source)}
            {ov.fx.unconverted.length > 0 && <Badge tone="warn">not converted: {ov.fx.unconverted.join(', ')}</Badge>}
            {!isNow && (
              <button type="button" onClick={() => setMonth(currentMonth())} className="press min-h-[36px] text-signal">
                Back to this month
              </button>
            )}
          </p>
        </>
      )}

      {sheet?.kind === 'recurring' && <RecurringSheet key={sheet.id} open={sheet.open} item={sheet.item} onClose={close} />}
      {sheet?.kind === 'budget' && <BudgetSheet key={sheet.id} open={sheet.open} category={sheet.category} current={sheet.current} onClose={close} />}
      {sheet?.kind === 'account' && <AccountSheet key={sheet.id} open={sheet.open} account={sheet.account} onClose={close} />}
      {sheet?.kind === 'sms' && <BankSmsSheet key={sheet.id} open={sheet.open} onClose={close} />}
      {sheet?.kind === 'forecast' && ov?.forecast && <ForecastSheet key={sheet.id} open={sheet.open} forecast={ov.forecast} income={ov.income.total} onClose={close} />}
    </div>
  );
}
