// Money page add-ons: the end-of-month forecast, detected subscriptions and savings goals.

import { useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, ErrorNote, Field, IconButton, SectionTitle, Segmented, cx, inputClass, riseStyle } from '../../components/ui';
import { formatMoney, isoDay } from '../../lib/apps';
import { announce, useTopic } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { parseAmount } from '../../lib/money';
import {
  GOAL_COLORS,
  GOAL_EMOJIS,
  cadenceLabel,
  confidenceLabel,
  forecastHeadline,
  goalPaceLabel,
  goalPercent,
  splitTracked,
} from '../../lib/moneyIntel';
import type { MoneyAccount, MoneyForecast, SavingsGoal, SubscriptionSuggestion } from '../../lib/types';

const MAD = 'MAD';
const CURRENCIES = ['MAD', 'EUR', 'USD', 'GBP'];

function dayShort(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

function mad(n: number): string {
  return formatMoney(Math.round(n), MAD);
}

// ------------------------------------------------------------------ forecast

/** One line in the Money hero: "~4,200 MAD left on the 31st", tap for how it is computed. */
export function ForecastLine({ forecast, onOpen }: { forecast: MoneyForecast; onOpen: () => void }) {
  const short = forecast.projected_left < 0;
  return (
    <button
      type="button"
      onClick={onOpen}
      className="press mt-4 flex min-h-[44px] w-full items-center gap-2 rounded-row bg-ink-850 px-3 py-2 text-left ring-1 ring-inset ring-ink-800"
      aria-label={`Forecast: ${forecastHeadline(forecast)}. How is this computed?`}
    >
      <Icon name="spark" size={16} className={cx('shrink-0', short ? 'text-danger-400' : 'text-signal')} />
      <span className="min-w-0 flex-1">
        <span className={cx('block text-[14px] font-semibold leading-snug tabular-nums', short ? 'text-danger-400' : 'text-ink-100')}>
          {forecastHeadline(forecast)}
        </span>
        <span className="block text-[12px] text-ink-500">{confidenceLabel(forecast.confidence)}</span>
      </span>
      <Icon name="right" size={16} className="shrink-0 text-ink-500" />
    </button>
  );
}

function Line({ label, value, strong, tone }: { label: string; value: string; strong?: boolean; tone?: string }) {
  return (
    <div className={cx('flex items-baseline justify-between gap-3 py-2', strong && 'border-t border-ink-700 pt-3')}>
      <span className={cx('text-[14px]', strong ? 'font-semibold text-ink-100' : 'text-ink-300')}>{label}</span>
      <span className={cx('shrink-0 font-display tabular-nums', strong ? 'text-[18px] font-semibold' : 'text-[15px]', tone ?? 'text-ink-100')}>{value}</span>
    </div>
  );
}

export function ForecastSheet({ open, onClose, forecast, income }: { open: boolean; onClose: () => void; forecast: MoneyForecast; income: number }) {
  const dayToDay = forecast.daily_avg * forecast.remaining_days;
  return (
    <Sheet open={open} onClose={onClose} title="End-of-month forecast">
      <div className="space-y-4">
        <div className="rounded-card bg-ink-850 px-4 py-2 ring-1 ring-inset ring-ink-800">
          <Line label="Spent so far" value={mad(forecast.spent_so_far)} />
          <Line label="Recurring still to come" value={`+ ${mad(forecast.remaining_recurring)}`} />
          <Line label={`Day-to-day: ${mad(forecast.daily_avg)} × ${forecast.remaining_days} days`} value={`+ ${mad(dayToDay)}`} />
          <Line label="Projected spending" value={mad(forecast.projected_spent)} strong />
          <Line label="Income this month" value={mad(income)} tone="text-mint-400" />
          <Line
            label={`Left on ${dayShort(forecast.month_end)}`}
            value={`${forecast.projected_left < 0 ? '-' : ''}${mad(Math.abs(forecast.projected_left))}`}
            strong
            tone={forecast.projected_left < 0 ? 'text-danger-400' : 'text-signal'}
          />
        </div>
        <div className="space-y-2 px-1 text-[13px] leading-relaxed text-ink-400">
          <p>
            The day-to-day average uses this month’s expenses that are not recurring charges
            {forecast.history_months > 0 && forecast.history_daily_avg !== null
              ? `, blended early in the month with your last ${forecast.history_months === 1 ? 'month' : `${forecast.history_months} months`} (${mad(forecast.history_daily_avg)} a day)`
              : ''}
            .
          </p>
          <p>A recurring charge already paid by an expense (same merchant, similar amount) counts once.</p>
          <p className="flex items-center gap-2">
            <Badge tone={forecast.confidence === 'high' ? 'ok' : forecast.confidence === 'medium' ? 'signal' : 'warn'}>{confidenceLabel(forecast.confidence)}</Badge>
            {forecast.confidence === 'low' ? 'Few days of data yet: it firms up as the month goes on.' : 'It updates with every expense.'}
          </p>
        </div>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ detected subscriptions

function SuggestionCard({ s, busy, onAccept, onDismiss, i }: { s: SubscriptionSuggestion; busy: boolean; onAccept: () => void; onDismiss: () => void; i: number }) {
  return (
    <div className="rise rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800" style={riseStyle(i)}>
      <div className="flex items-start gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-signal/15 text-signal">
          <Icon name="repeat" size={18} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[15px] text-ink-100">{s.name}</p>
          <p className="truncate text-[12px] text-ink-500">
            {cadenceLabel(s.cadence)} · seen {s.occurrences}× · next {dayShort(s.next_date)}
          </p>
        </div>
        <div className="shrink-0 text-right">
          <p className="font-display text-[16px] font-semibold tabular-nums text-ink-100">~{formatMoney(s.avg_amount, s.currency)}</p>
          <Badge tone={s.level === 'high' ? 'ok' : s.level === 'medium' ? 'signal' : 'neutral'}>{Math.round(s.confidence * 100)}% sure</Badge>
        </div>
      </div>
      <div className="mt-3 flex gap-2">
        <Button variant="primary" icon="check" className="flex-1" onClick={onAccept} disabled={busy}>
          Track it
        </Button>
        <Button variant="ghost" onClick={onDismiss} disabled={busy}>
          Not a subscription
        </Button>
      </div>
    </div>
  );
}

/** Suggestions from the expenses + the weekly / yearly subscriptions already tracked. Current month only.
 *  The two load apart: a failed suggestions scan never hides what is already tracked. */
export function SubscriptionsSection({ i }: { i: number }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const suggestions = useHubData((c, signal) => c.subscriptionSuggestions(signal), []);
  const trackedData = useHubData(async (c, signal) => splitTracked(await c.trackedSubscriptions(signal)), []);
  const reload = () => {
    suggestions.reload();
    trackedData.reload();
  };
  useTopic(['money', 'expenses'], reload);
  const [busy, setBusy] = useState<string | null>(null);
  const [showDismissed, setShowDismissed] = useState(false);

  const act = async (key: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await fn();
      notify(done, 'ok');
      announce('money');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const list = {
    suggestions: suggestions.data ?? [],
    tracked: trackedData.data?.tracked ?? [],
    dismissed: trackedData.data?.dismissed ?? [],
  };
  const error = suggestions.error ?? trackedData.error;
  if (!error && !list.suggestions.length && !list.tracked.length && !list.dismissed.length) return null;
  return (
    <section className="rise" style={riseStyle(i)}>
      <SectionTitle>Subscriptions</SectionTitle>
      {error && (
        <div className="mb-2">
          <ErrorNote message={suggestions.error ? `Could not scan for subscriptions: ${error}` : error} onRetry={reload} />
        </div>
      )}
      {list.suggestions.length > 0 && (
        <>
          <p className="px-1 pb-2 text-[13px] text-ink-400">Spotted in your expenses. Track them to get a push 2 days before each charge.</p>
          <div className="space-y-2">
            {list.suggestions.map((s, idx) => (
              <SuggestionCard
                key={s.key}
                s={s}
                i={idx}
                busy={busy === s.key}
                onAccept={() => void act(s.key, () => client.acceptSubscription({ key: s.key }), `${s.name} is tracked`)}
                onDismiss={() => void act(s.key, () => client.dismissSubscription(s.key), `${s.name} hidden: restore it under “Not subscriptions”`)}
              />
            ))}
          </div>
        </>
      )}
      {list.tracked.length > 0 && (
        <div className="mt-2 divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {list.tracked.map((t) => (
            <div key={t.id} className="flex min-h-[56px] items-center gap-3 px-4 py-2">
              <Icon name="repeat" size={18} className="shrink-0 text-ink-400" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{t.name}</span>
                <span className="block truncate text-[12px] text-ink-500">
                  {cadenceLabel(t.cadence)}
                  {t.next_date ? ` · next ${dayShort(t.next_date)}` : ''}
                </span>
              </span>
              <span className="shrink-0 font-display text-[15px] font-semibold tabular-nums text-ink-100">{formatMoney(t.amount, t.currency)}</span>
              <IconButton
                icon="x"
                label={`Stop tracking ${t.name}`}
                disabled={busy === t.id}
                onClick={async () => {
                  if (!(await confirm({ title: `Stop tracking ${t.name}?`, body: 'No more bill reminders for it. It can be suggested again later.', confirmLabel: 'Stop tracking', tone: 'danger' }))) return;
                  void act(t.id, () => client.forgetSubscription(t.id), `${t.name} is no longer tracked`);
                }}
              />
            </div>
          ))}
        </div>
      )}
      {list.dismissed.length > 0 && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowDismissed((v) => !v)}
            aria-expanded={showDismissed}
            className="press flex min-h-[44px] w-full items-center gap-2 px-1 text-left text-[13px] text-ink-400"
          >
            <Icon name="right" size={14} className={cx('shrink-0 transition-transform', showDismissed && 'rotate-90')} />
            Not subscriptions ({list.dismissed.length})
          </button>
          {showDismissed && (
            <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
              {list.dismissed.map((t) => (
                <div key={t.id} className="flex min-h-[52px] items-center gap-3 px-4 py-2">
                  <span className="min-w-0 flex-1 truncate text-[14px] text-ink-300">{t.name}</span>
                  <Button
                    variant="ghost"
                    icon="undo"
                    disabled={busy === t.id}
                    onClick={() => void act(t.id, () => client.forgetSubscription(t.id), `${t.name} can be suggested again`)}
                  >
                    Restore
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ savings goals

function GoalBar({ goal }: { goal: SavingsGoal }) {
  const color = goal.color ?? '#F5B43C';
  return (
    <div className="h-2 overflow-hidden rounded-full bg-ink-800" role="img" aria-label={`${goalPercent(goal)}% saved`}>
      <div className="h-full rounded-full transition-[width] duration-slow ease-out" style={{ width: `${Math.max(2, goal.progress * 100)}%`, background: color }} />
    </div>
  );
}

type GoalSheetState = { kind: 'edit'; goal: SavingsGoal | null } | { kind: 'contribute'; goal: SavingsGoal };

export function GoalsSection({ accounts, i }: { accounts: MoneyAccount[]; i: number }) {
  const data = useHubData((c, s) => c.goals(s), []);
  useTopic(['money'], data.reload);
  const [sheet, setSheet] = useState<(GoalSheetState & { id: number; open: boolean }) | null>(null);
  const [seq, setSeq] = useState(1);
  const open = (s: GoalSheetState) => {
    setSheet({ ...s, id: seq, open: true });
    setSeq((n) => n + 1);
  };
  const close = () => setSheet((s) => (s ? { ...s, open: false } : s));
  const goals = data.data?.goals ?? [];

  return (
    <section className="rise" style={riseStyle(i)}>
      <SectionTitle action={<IconButton icon="plus" label="New savings goal" onClick={() => open({ kind: 'edit', goal: null })} />}>
        Savings goals
        {goals.length > 0 && <span className="pl-2 font-display text-[15px] font-semibold text-signal">{mad(data.data?.total_saved_mad ?? 0)}</span>}
      </SectionTitle>
      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
      {goals.length ? (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {goals.map((g, idx) => (
            <button
              key={g.id}
              type="button"
              onClick={() => open({ kind: 'contribute', goal: g })}
              className="press rise block w-full px-4 py-3 text-left hover:bg-ink-850"
              style={riseStyle(idx)}
            >
              <span className="flex items-center gap-3">
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full text-[18px]" style={{ background: `${g.color ?? '#F5B43C'}22` }}>
                  {g.emoji ?? '🎯'}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[15px] text-ink-100">{g.name}</span>
                  <span className={cx('block truncate text-[12px]', g.overdue ? 'text-danger-400' : g.done ? 'text-mint-400' : 'text-ink-500')}>{goalPaceLabel(g)}</span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="block font-display text-[15px] font-semibold tabular-nums text-ink-100">{formatMoney(g.saved, g.currency)}</span>
                  <span className="block text-[12px] tabular-nums text-ink-500">of {formatMoney(g.target, g.currency)}</span>
                </span>
              </span>
              <span className="mt-2.5 flex items-center gap-2">
                <span className="min-w-0 flex-1">
                  <GoalBar goal={g} />
                </span>
                <span className="w-9 shrink-0 text-right text-[12px] tabular-nums text-ink-400">{goalPercent(g)}%</span>
              </span>
            </button>
          ))}
        </div>
      ) : (
        !data.loading && (
          <button
            type="button"
            onClick={() => open({ kind: 'edit', goal: null })}
            className="press w-full rounded-card border border-dashed border-ink-700 px-4 py-4 text-left text-[14px] text-ink-400"
          >
            Saving for a trip, a laptop or a safety net? Set a target and see what to put aside each month.
          </button>
        )
      )}
      {sheet?.kind === 'edit' && <GoalSheet key={sheet.id} open={sheet.open} goal={sheet.goal} accounts={accounts} onClose={close} />}
      {sheet?.kind === 'contribute' && (
        <ContributeSheet key={sheet.id} open={sheet.open} goal={sheet.goal} onClose={close} onEdit={() => open({ kind: 'edit', goal: sheet.goal })} />
      )}
    </section>
  );
}

function useSave(onClose: () => void) {
  const { notify } = useHub();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<unknown>, done: string) => {
    setSaving(true);
    setError(null);
    try {
      await fn();
      announce('money');
      notify(done, 'ok');
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };
  return { saving, error, setError, run };
}

function AmountInput({ label, value, onChange, currency, onCurrency, autoFocus }: { label: string; value: string; onChange: (v: string) => void; currency?: string; onCurrency?: (v: string) => void; autoFocus?: boolean }) {
  return (
    <div className="flex items-end gap-2">
      <Field label={label}>
        <input
          inputMode="decimal"
          value={value}
          autoFocus={autoFocus}
          onChange={(e) => onChange(e.target.value.replace(/[^\d.,\s]/g, ''))}
          placeholder="0.00"
          className={cx(inputClass, 'font-display text-[28px] font-semibold')}
        />
      </Field>
      {onCurrency && (
        <label className="w-28 shrink-0">
          <span className="sr-only">Currency</span>
          <input list="goal-currencies" value={currency} onChange={(e) => onCurrency(e.target.value.toUpperCase().slice(0, 3))} className={cx(inputClass, 'text-center font-semibold')} />
          <datalist id="goal-currencies">
            {CURRENCIES.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </label>
      )}
    </div>
  );
}

export function GoalSheet({ open, onClose, goal, accounts }: { open: boolean; onClose: () => void; goal: SavingsGoal | null; accounts: MoneyAccount[] }) {
  const { client } = useHub();
  const confirm = useConfirm();
  const [name, setName] = useState(goal?.name ?? '');
  const [emoji, setEmoji] = useState(goal?.emoji ?? GOAL_EMOJIS[0]);
  const [color, setColor] = useState(goal?.color ?? GOAL_COLORS[0]);
  const [target, setTarget] = useState(goal ? String(goal.target) : '');
  const [currency, setCurrency] = useState(goal?.currency ?? MAD);
  const [saved, setSaved] = useState('');
  const [deadline, setDeadline] = useState(goal?.deadline ?? '');
  const [accountId, setAccountId] = useState(goal?.account_id ?? '');
  const { saving, error, setError, run } = useSave(onClose);

  const save = () => {
    const value = parseAmount(target);
    if (!name.trim()) {
      setError('Name the goal (Trip to Japan, New laptop…).');
      return;
    }
    if (value === null || value <= 0) {
      setError('Enter the amount you want to reach.');
      return;
    }
    const start = saved ? parseAmount(saved) : 0;
    if (start === null || start < 0) {
      setError('“Already saved” must be an amount.');
      return;
    }
    const fields = {
      name: name.trim(),
      target: value,
      currency: currency.trim().toUpperCase() || MAD,
      deadline: deadline || null,
      account_id: accountId || null,
      emoji,
      color,
    };
    void run(() => (goal ? client.updateGoal(goal.id, fields) : client.createGoal({ ...fields, saved: start })), goal ? 'Goal updated' : 'Goal added');
  };

  const remove = async () => {
    if (!goal) return;
    if (!(await confirm({ title: `Delete ${goal.name}?`, body: 'Its contributions are deleted too. Your accounts are not touched.', confirmLabel: 'Delete goal', tone: 'danger' }))) return;
    void run(() => client.deleteGoal(goal.id), 'Goal deleted');
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={goal ? 'Edit goal' : 'New savings goal'}
      headerAction={goal ? <IconButton icon="trash" label="Delete goal" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : goal ? 'Save goal' : 'Add goal'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Trip to Japan, New laptop…" className={inputClass} autoFocus={!goal} />
        </Field>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Emoji">
          {GOAL_EMOJIS.map((e) => (
            <Chip key={e} active={emoji === e} onClick={() => setEmoji(e)}>
              <span className="text-[17px]">{e}</span>
            </Chip>
          ))}
        </div>
        <AmountInput label="Target" value={target} onChange={setTarget} currency={currency} onCurrency={setCurrency} />
        {!goal && <AmountInput label="Already saved (optional)" value={saved} onChange={setSaved} />}
        <Field label="Deadline (optional)" hint="With a deadline you see how much to put aside each month.">
          <input type="date" value={deadline} min={isoDay()} onChange={(e) => setDeadline(e.target.value)} className={inputClass} />
        </Field>
        {accounts.length > 0 && (
          <Field label="Kept on (optional)">
            <select value={accountId} onChange={(e) => setAccountId(e.target.value)} className={inputClass}>
              <option value="">No account</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </Field>
        )}
        <div className="flex gap-3 px-1" role="radiogroup" aria-label="Colour">
          {GOAL_COLORS.map((c) => (
            <button
              key={c}
              type="button"
              role="radio"
              aria-checked={color === c}
              aria-label={`Colour ${c}`}
              onClick={() => setColor(c)}
              className={cx('press h-9 w-9 rounded-full ring-offset-2 ring-offset-ink-900', color === c && 'ring-2 ring-ink-100')}
              style={{ background: c }}
            />
          ))}
        </div>
      </div>
    </Sheet>
  );
}

export function ContributeSheet({ open, onClose, goal, onEdit }: { open: boolean; onClose: () => void; goal: SavingsGoal; onEdit: () => void }) {
  const { client } = useHub();
  const [direction, setDirection] = useState<'add' | 'take'>('add');
  const [amount, setAmount] = useState(goal.per_month_needed && !goal.done ? String(Math.ceil(goal.per_month_needed)) : '');
  const [note, setNote] = useState('');
  const history = useHubData((c, s) => c.goal(goal.id, s), [goal.id]);
  const { saving, error, setError, run } = useSave(onClose);

  const save = () => {
    const value = parseAmount(amount);
    if (value === null || value <= 0) {
      setError('Enter an amount.');
      return;
    }
    if (direction === 'take' && value > goal.saved) {
      setError(`Only ${formatMoney(goal.saved, goal.currency)} saved so far.`);
      return;
    }
    const signed = direction === 'add' ? value : -value;
    void run(() => client.contributeGoal(goal.id, signed, note.trim() || null), direction === 'add' ? `Added to ${goal.name}` : `Taken from ${goal.name}`);
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={`${goal.emoji ?? '🎯'} ${goal.name}`}
      headerAction={<IconButton icon="edit" label="Edit goal" onClick={() => { onClose(); onEdit(); }} />}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : direction === 'add' ? 'Add money' : 'Take money out'}
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="space-y-2 rounded-card bg-ink-850 p-4 ring-1 ring-inset ring-ink-800">
          <p className="flex items-baseline justify-between gap-2">
            <span className="font-display text-[22px] font-semibold tabular-nums text-ink-100">{formatMoney(goal.saved, goal.currency)}</span>
            <span className="text-[13px] tabular-nums text-ink-400">of {formatMoney(goal.target, goal.currency)}</span>
          </p>
          <GoalBar goal={goal} />
          <p className={cx('text-[13px]', goal.overdue ? 'text-danger-400' : goal.done ? 'text-mint-400' : 'text-ink-400')}>
            {goalPaceLabel(goal)}
            {goal.deadline && !goal.done ? ` · by ${dayShort(goal.deadline)}` : ''}
            {goal.account ? ` · on ${goal.account.name}` : ''}
          </p>
        </div>
        {error && <ErrorNote message={error} />}
        <Segmented
          value={direction}
          onChange={setDirection}
          options={[
            { value: 'add', label: 'Add' },
            { value: 'take', label: 'Take out' },
          ]}
        />
        <AmountInput label={`Amount (${goal.currency})`} value={amount} onChange={setAmount} autoFocus />
        <Field label="Note">
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" className={inputClass} />
        </Field>
        {history.data && history.data.contributions.length > 0 && (
          <div>
            <p className="px-1 pb-1 text-[13px] text-ink-300">History</p>
            <ul className="divide-y divide-ink-800 rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
              {history.data.contributions.slice(0, 8).map((c) => (
                <li key={c.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-[14px]">
                  <span className="min-w-0 truncate text-ink-300">
                    {dayShort(c.date)}
                    {c.note ? ` · ${c.note}` : ''}
                  </span>
                  <span className={cx('shrink-0 font-display font-semibold tabular-nums', c.amount < 0 ? 'text-danger-400' : 'text-mint-400')}>
                    {c.amount < 0 ? '-' : '+'}
                    {formatMoney(Math.abs(c.amount), goal.currency)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Sheet>
  );
}
