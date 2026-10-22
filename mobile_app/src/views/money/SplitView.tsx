import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, EmptyState, ErrorNote, Field, IconButton, Segmented, SkeletonList, cx, inputClass, riseStyle } from '../../components/ui';
import { formatMoney, isoDay } from '../../lib/apps';
import { announce, useTopic } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { parseAmount } from '../../lib/money';
import { autofillShares, computeShares, memberNames, myBalanceLabel, pairLabel, shareDebtLines, type ShareDraft } from '../../lib/moneyTools';
import type { SplitEntry, SplitGroup, SplitGroupSummary, SplitMode, SplitPair } from '../../lib/moneyTypes';
import { useNav, usePullRefresh } from '../../lib/nav';
import { EXPENSE_CATEGORIES, type ExpenseCategory } from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';
import { clearSplitGroup, peekSplitGroup } from './splitLink';

function dayShort(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

const cleanNumber = (v: string) => v.replace(/[^\d.,\s]/g, '');

/** Typed text to a number, null when empty (the calculator treats empty and 0 differently for auto-fill). */
function typed(v: string | undefined): number | null {
  return v && v.trim() ? parseAmount(v) : null;
}

// ------------------------------------------------------------------ group sheet (new / edit)

function GroupSheet({ open, onClose, group, onSaved }: { open: boolean; onClose: () => void; group: SplitGroup | null; onSaved: (g: SplitGroup | null) => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [name, setName] = useState(group?.name ?? '');
  const [currency, setCurrency] = useState(group?.currency ?? 'MAD');
  const [members, setMembers] = useState<{ id?: string; name: string; me?: boolean }[]>(
    group ? group.members.map((m) => ({ id: m.id, name: m.name, me: m.me })) : [{ name: 'Me', me: true }, { name: '' }],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const clean = members.map((m) => ({ ...m, name: m.name.trim() })).filter((m) => m.name);
    if (!name.trim()) return setError('Name the group (a trip, the flat…).');
    if (clean.filter((m) => !m.me).length < 1) return setError('Add at least one other person.');
    setBusy(true);
    setError(null);
    try {
      const me = clean.find((m) => m.me);
      const saved = group
        ? await client.updateSplitGroup(group.id, { name: name.trim(), currency: currency.trim().toUpperCase() || 'MAD', members: clean.map(({ id, name: n }) => ({ id, name: n })) })
        : await client.createSplitGroup({ name: name.trim(), currency: currency.trim().toUpperCase() || 'MAD', me_name: me?.name || 'Me', members: clean.filter((m) => !m.me).map((m) => m.name) });
      announce('money');
      notify(group ? 'Group saved' : 'Group created', 'ok');
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!group) return;
    const body = 'Its shared expenses go too, your shares leave your expenses, and what people owe you (or you owe them) in this group leaves the Debts page, repayments included.';
    if (!(await confirm({ title: `Delete ${group.name}?`, body, confirmLabel: 'Delete group', tone: 'danger' }))) return;
    try {
      await client.deleteSplitGroup(group.id);
      announce('money', 'expenses');
      notify('Group deleted', 'ok');
      onSaved(null);
      onClose();
    } catch (err) {
      setError(errorText(err));
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={group ? 'Edit group' : 'New group'}
      headerAction={group ? <IconButton icon="trash" label="Delete group" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={() => void save()} disabled={busy}>
          {busy ? 'Saving…' : group ? 'Save group' : 'Create group'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <div className="grid grid-cols-[1fr_96px] gap-2">
          <Field label="Name">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Chefchaouen trip" className={inputClass} autoFocus={!group} />
          </Field>
          <Field label="Currency">
            <input value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase().slice(0, 3))} disabled={!!group?.entries.length} className={cx(inputClass, 'text-center font-semibold')} />
          </Field>
        </div>
        <div className="space-y-2">
          <span className="block px-1 text-[13px] text-ink-300">People</span>
          {members.map((m, i) => (
            <div key={m.id ?? `new-${i}`} className="flex items-center gap-2">
              <input
                aria-label={m.me ? 'Your name in the group' : 'Name'}
                value={m.name}
                onChange={(e) => setMembers((list) => list.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                placeholder={m.me ? 'Me' : 'Name'}
                className={cx(inputClass, 'min-w-0 flex-1')}
              />
              {m.me ? (
                <Badge tone="signal">you</Badge>
              ) : (
                <IconButton icon="x" label="Remove person" onClick={() => setMembers((list) => list.filter((_, j) => j !== i))} />
              )}
            </div>
          ))}
          <Button variant="ghost" icon="plus" onClick={() => setMembers((list) => [...list, { name: '' }])}>
            Add a person
          </Button>
        </div>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ shared expense sheet

function initialFields(entry: SplitEntry | null, ids: string[], mode: SplitMode): Record<string, string> {
  return Object.fromEntries(
    ids.map((id) => {
      if (!entry) return [id, ''];
      if (mode === 'custom' && entry.split === 'custom' && entry.shares[id]) return [id, String(entry.shares[id])];
      if (mode === 'percent' && entry.split === 'percent' && entry.percents?.[id]) return [id, String(entry.percents[id])];
      return [id, ''];
    }),
  );
}

function EntrySheet({ open, onClose, group, entry, onSaved }: { open: boolean; onClose: () => void; group: SplitGroup; entry: SplitEntry | null; onSaved: (g: SplitGroup) => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const names = memberNames(group.members, 'Me');
  const debtNames = memberNames(group.members);
  const ids = useMemo(() => group.members.map((m) => m.id), [group.members]);
  const [description, setDescription] = useState(entry?.description ?? '');
  const [amount, setAmount] = useState(entry ? String(entry.amount) : '');
  const [paidBy, setPaidBy] = useState(entry?.paid_by ?? group.me);
  const [date, setDate] = useState(entry?.date ?? isoDay());
  const [category, setCategory] = useState<ExpenseCategory>(entry?.category ?? 'food');
  const [mode, setMode] = useState<SplitMode>(entry?.split ?? 'equal');
  const [included, setIncluded] = useState<string[]>(entry && entry.kind === 'expense' ? ids.filter((id) => id in entry.shares) : ids);
  const [amounts, setAmounts] = useState<Record<string, string>>(() => initialFields(entry, ids, 'custom'));
  const [percents, setPercents] = useState<Record<string, string>>(() => initialFields(entry, ids, 'percent'));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const settlement = entry?.kind === 'settlement';

  const value = parseAmount(amount) ?? 0;
  const draft: ShareDraft = {
    amount: value,
    mode,
    order: ids,
    included,
    payer: paidBy,
    amounts: Object.fromEntries(ids.map((id) => [id, typed(amounts[id])])),
    percents: Object.fromEntries(ids.map((id) => [id, typed(percents[id])])),
  };
  const result = computeShares(draft);
  const debtLines = result.error ? [] : shareDebtLines(result.shares, paidBy, group.me, debtNames);
  const canFill = mode !== 'equal' && autofillShares(draft) !== null;

  const toggle = (id: string) => setIncluded((list) => (list.includes(id) ? list.filter((x) => x !== id) : ids.filter((x) => x === id || list.includes(x))));
  const fill = () => {
    const filled = autofillShares(draft);
    if (!filled) return;
    const text = Object.fromEntries(Object.entries(filled).map(([id, v]) => [id, v ? String(v) : '']));
    if (mode === 'custom') setAmounts((a) => ({ ...a, ...text }));
    else setPercents((p) => ({ ...p, ...text }));
  };

  const save = async () => {
    if (result.error) return setError(result.error);
    setBusy(true);
    setError(null);
    const inc = ids.filter((id) => included.includes(id));
    const body = {
      description: description.trim(),
      amount: value,
      paid_by: paidBy,
      date,
      category,
      split: mode,
      ...(mode === 'equal'
        ? { participants: inc }
        : mode === 'custom'
          ? { shares: result.shares }
          : { percents: Object.fromEntries(inc.map((id) => [id, draft.percents[id] ?? 0]).filter(([, v]) => (v as number) > 0)) }),
    };
    try {
      const res = entry ? await client.updateSplitEntry(group.id, entry.id, body) : await client.addSplitEntry(group.id, body);
      announce('money', 'expenses');
      notify(entry ? 'Saved' : 'Expense added', 'ok');
      onSaved(res.group);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!entry) return;
    const body = settlement ? 'It is removed from the Debts page too.' : 'Your share leaves your expenses and what it put on the Debts page goes too.';
    if (!(await confirm({ title: settlement ? 'Delete this repayment?' : `Delete ${entry.description || 'this expense'}?`, body, confirmLabel: 'Delete', tone: 'danger' }))) return;
    try {
      onSaved(await client.deleteSplitEntry(group.id, entry.id));
      announce('money', 'expenses');
      onClose();
    } catch (err) {
      setError(errorText(err));
    }
  };

  if (settlement && entry) {
    return (
      <Sheet open={open} onClose={onClose} title="Repayment" footer={<Button variant="danger" className="w-full" onClick={() => void remove()}>Delete repayment</Button>}>
        <div className="space-y-3">
          {error && <ErrorNote message={error} />}
          <p className="text-[15px] text-ink-200">
            {names[entry.paid_by]} paid {names[Object.keys(entry.shares)[0]] ?? '?'} back {formatMoney(entry.amount, group.currency)} on {dayShort(entry.date)}.
          </p>
          {entry.origin === 'voice' && <p className="text-[13px] text-ink-500">Recorded by voice.</p>}
        </div>
      </Sheet>
    );
  }

  const unitLabel = mode === 'percent' ? '%' : group.currency;
  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={entry ? 'Edit shared expense' : 'Shared expense'}
      headerAction={entry ? <IconButton icon="trash" label="Delete shared expense" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={() => void save()} disabled={busy || !!result.error}>
          {busy ? 'Saving…' : entry ? 'Save' : 'Add expense'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <Field label={`Amount (${group.currency})`}>
          <input inputMode="decimal" value={amount} autoFocus={!entry} onChange={(e) => setAmount(cleanNumber(e.target.value))} placeholder="0.00" className={cx(inputClass, 'font-display text-[28px] font-semibold')} />
        </Field>
        <Field label="What">
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Riad, dinner, taxi…" className={inputClass} />
        </Field>
        <div className="space-y-1.5">
          <span className="block px-1 text-[13px] text-ink-300">Paid by</span>
          <div className="flex flex-wrap gap-2">
            {group.members.map((m) => (
              <Chip key={m.id} active={paidBy === m.id} onClick={() => setPaidBy(m.id)}>
                {names[m.id]}
              </Chip>
            ))}
          </div>
        </div>
        <div className="space-y-2">
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { value: 'equal', label: 'Equal' },
              { value: 'custom', label: 'Amounts' },
              { value: 'percent', label: '%' },
            ]}
          />
          <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850 ring-1 ring-inset ring-ink-700" aria-label="Shares">
            {group.members.map((m) => {
              const on = included.includes(m.id);
              const share = result.shares[m.id];
              return (
                <li key={m.id} className="flex min-h-[52px] items-center gap-2 pr-3">
                  <button
                    type="button"
                    aria-pressed={on}
                    aria-label={`${on ? 'Leave out' : 'Include'} ${names[m.id]}`}
                    onClick={() => toggle(m.id)}
                    className="press grid h-[52px] w-11 shrink-0 place-items-center"
                  >
                    <Icon name={on ? 'check' : 'circle'} size={18} className={on ? 'text-signal' : 'text-ink-500'} />
                  </button>
                  <span className={cx('min-w-0 flex-1 truncate text-[15px]', on ? 'text-ink-100' : 'text-ink-500 line-through')}>{names[m.id]}</span>
                  {mode !== 'equal' && (
                    <label className="relative w-[92px] shrink-0">
                      <span className="sr-only">
                        {names[m.id]} ({unitLabel})
                      </span>
                      <input
                        inputMode="decimal"
                        disabled={!on}
                        value={on ? (mode === 'custom' ? amounts[m.id] : percents[m.id]) ?? '' : ''}
                        onChange={(e) => {
                          const v = cleanNumber(e.target.value);
                          if (mode === 'custom') setAmounts((a) => ({ ...a, [m.id]: v }));
                          else setPercents((p) => ({ ...p, [m.id]: v }));
                        }}
                        placeholder="0"
                        className={cx(inputClass, 'min-h-[40px] py-1.5 pr-7 text-right disabled:opacity-40')}
                      />
                      <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[12px] text-ink-500">{mode === 'percent' ? '%' : ''}</span>
                    </label>
                  )}
                  {mode !== 'custom' && (
                    <span className="w-[78px] shrink-0 text-right text-[13px] tabular-nums text-ink-400">{on && share !== undefined ? share.toFixed(2) : '-'}</span>
                  )}
                </li>
              );
            })}
          </ul>
          <div className="flex min-h-[40px] items-center justify-between gap-2 px-1" aria-live="polite">
            <p className={cx('text-[13px]', result.error ? 'text-amber-400' : 'text-mint-400')}>
              {result.error ?? (mode === 'equal' ? `${included.length} ${included.length === 1 ? 'person' : 'people'}, leftover cents to the payer` : 'Adds up ✓')}
            </p>
            {canFill && (
              <Button variant="ghost" onClick={fill} className="shrink-0">
                Fill the rest
              </Button>
            )}
          </div>
        </div>
        {debtLines.length > 0 && (
          <div className="rounded-card bg-ink-900 px-3 py-2 ring-1 ring-inset ring-ink-800">
            <p className="pb-1 text-[12px] text-ink-500">On the Debts page</p>
            <ul className="space-y-0.5 text-[14px] text-ink-200">
              {debtLines.map((l) => (
                <li key={l}>
                  {l} {group.currency}
                </li>
              ))}
            </ul>
          </div>
        )}
        <Field label="Date">
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className={inputClass} />
        </Field>
        <div className="space-y-1.5">
          <span className="block px-1 text-[13px] text-ink-300">Category</span>
          <div className="flex flex-wrap gap-2">
            {EXPENSE_CATEGORIES.map((c) => (
              <Chip key={c} active={category === c} onClick={() => setCategory(c)}>
                <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[c] }} />
                {c}
              </Chip>
            ))}
          </div>
        </div>
        <p className="px-1 text-[12px] text-ink-500">Only your share is added to your expenses and budgets, whoever paid; repayments between you are transfers, not expenses.</p>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ settle with one person

function SettleSheet({ open, onClose, group, pair, onSaved }: { open: boolean; onClose: () => void; group: SplitGroup; pair: SplitPair; onSaved: (g: SplitGroup) => void }) {
  const { client, notify } = useHub();
  const owed = Math.abs(pair.net);
  const theyPay = pair.net > 0;
  const [amount, setAmount] = useState(owed.toFixed(2));
  const [date, setDate] = useState(isoDay());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const value = parseAmount(amount);
    if (value === null || value <= 0) return setError('Enter the amount paid back.');
    if (Math.round(value * 100) > Math.round(owed * 100)) return setError(`At most ${formatMoney(owed, group.currency)}.`);
    setBusy(true);
    setError(null);
    try {
      const fresh = await client.settleSplit(group.id, theyPay ? { from: pair.member, to: group.me, amount: value, date } : { from: group.me, to: pair.member, amount: value, date });
      announce('money');
      notify(Math.round(value * 100) === Math.round(owed * 100) ? `Settled with ${pair.name} ✓` : 'Repayment recorded', 'ok');
      onSaved(fresh);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={theyPay ? `${pair.name} paid you back` : `You paid ${pair.name} back`}
      footer={
        <Button variant="primary" className="w-full" onClick={() => void save()} disabled={busy}>
          {busy ? 'Saving…' : 'Record repayment'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <p className="text-[15px] text-ink-200">{pairLabel(pair, group.currency)} in this group.</p>
        <Field label={`Amount (${group.currency})`}>
          <input inputMode="decimal" value={amount} onChange={(e) => setAmount(cleanNumber(e.target.value))} className={cx(inputClass, 'font-display text-[24px] font-semibold')} />
        </Field>
        <Field label="Date">
          <input type="date" value={date} max={isoDay()} onChange={(e) => setDate(e.target.value)} className={inputClass} />
        </Field>
        <p className="px-1 text-[12px] text-ink-500">A transfer, not an expense. It shows as a repayment on the Debts page too.</p>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ one group

function PairRow({ pair, currency, onSettle, i }: { pair: SplitPair; currency: string; onSettle: () => void; i: number }) {
  const [openRow, setOpenRow] = useState(false);
  const tone = pair.net > 0.004 ? 'text-signal' : pair.net < -0.004 ? 'text-danger-400' : 'text-ink-500';
  return (
    <li className="rise" style={riseStyle(i)}>
      <div className="flex min-h-[60px] items-center gap-2 pl-4 pr-2">
        <button type="button" onClick={() => setOpenRow((v) => !v)} aria-expanded={openRow} className="press flex min-h-[52px] min-w-0 flex-1 items-center gap-3 text-left">
          <span className={cx('grid h-9 w-9 shrink-0 place-items-center rounded-full font-display text-[14px] font-semibold', pair.net > 0.004 ? 'bg-signal/15 text-signal' : pair.net < -0.004 ? 'bg-danger-400/15 text-danger-400' : 'bg-ink-800 text-ink-400')}>
            {pair.name.slice(0, 1).toUpperCase()}
          </span>
          <span className="min-w-0 flex-1">
            <span className={cx('block truncate text-[15px]', pair.net === 0 ? 'text-ink-400' : 'text-ink-100')}>
              {pair.net > 0.004 ? `${pair.name} owes you` : pair.net < -0.004 ? `You owe ${pair.name}` : `All settled with ${pair.name}`}
            </span>
            {Math.abs(pair.net) > 0.004 && <span className={cx('block font-display text-[16px] font-semibold tabular-nums', tone)}>{formatMoney(Math.abs(pair.net), currency)}</span>}
            <span className="block truncate text-[12px] text-ink-500">
              {pair.items.length} line{pair.items.length === 1 ? '' : 's'}
              {pair.paid_me + pair.i_paid > 0 ? ` · ${formatMoney(pair.paid_me + pair.i_paid, currency)} paid back` : ''}
            </span>
          </span>
          <Icon name={openRow ? 'up' : 'down'} size={14} className="shrink-0 text-ink-500" />
        </button>
        {Math.abs(pair.net) > 0.004 ? (
          <Button variant="primary" onClick={onSettle} className="shrink-0">
            Settle
          </Button>
        ) : (
          <span className={cx('shrink-0 px-2 text-[13px]', tone)}>✓</span>
        )}
      </div>
      {openRow && pair.items.length > 0 && (
        <ul className="space-y-1 px-4 pb-3">
          {pair.items.map((it) => (
            <li key={`${it.entry_id}`} className="flex items-baseline justify-between gap-2 text-[13px]">
              <span className="min-w-0 truncate text-ink-400">
                {dayShort(it.date)} · {it.kind === 'settlement' ? 'Repayment' : it.description || 'Expense'}
              </span>
              <span className={cx('shrink-0 tabular-nums', it.effect > 0 ? 'text-signal' : 'text-danger-400')}>
                {it.effect > 0 ? '+' : '−'}
                {formatMoney(Math.abs(it.effect), currency)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

type GroupSheetState = { id: number; open: boolean; kind: 'entry' | 'group' | 'settle'; entry: SplitEntry | null; pair?: SplitPair };

function GroupDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { client, notify } = useHub();
  const { go } = useNav();
  const confirm = useConfirm();
  const data = useHubData((c, s) => c.splitGroup(id, s), [id]);
  useTopic(['money'], data.reload);
  usePullRefresh(data.reload);
  const [sheet, setSheet] = useState<GroupSheetState | null>(null);
  const next = useRef(1);
  const g = data.data;
  const set = (fresh: SplitGroup | null) => (fresh ? data.setData(() => fresh) : onBack());
  const close = () => setSheet((s) => (s ? { ...s, open: false } : s));

  const settleOthers = async (from: string, to: string, amount: number, label: string) => {
    if (!g) return;
    if (!(await confirm({ title: `${label}?`, body: `Records ${formatMoney(amount, g.currency)} paid back. It is a transfer, not an expense.`, confirmLabel: 'Record repayment', tone: 'primary' }))) return;
    try {
      set(await client.settleSplit(g.id, { from, to, amount }));
      announce('money');
      notify('Repayment recorded', 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  if (!g) {
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-1">
          <IconButton icon="left" label="All groups" onClick={onBack} />
        </div>
        {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
        {data.loading && <SkeletonList rows={3} />}
      </div>
    );
  }
  const names = memberNames(g.members);
  const pairs = g.pairs ?? [];
  const others = g.settle.filter((s) => s.from !== g.me && s.to !== g.me);
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-1">
        <IconButton icon="left" label="All groups" onClick={onBack} />
        <h2 className="min-w-0 flex-1 truncate font-display text-[22px] font-bold text-ink-100">{g.name}</h2>
        <IconButton icon="edit" label="Edit group" onClick={() => setSheet({ id: next.current++, open: true, kind: 'group', entry: null })} />
      </div>
      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}

      <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
        <p className={cx('font-display text-[22px] font-semibold', g.totals.my_net > 0 ? 'text-signal' : g.totals.my_net < 0 ? 'text-danger-400' : 'text-mint-400')}>
          {myBalanceLabel(g.totals.my_net, g.currency)}
        </p>
        <p className="pt-1 text-[13px] text-ink-400">
          Group spent {formatMoney(g.totals.spent, g.currency)} · your share {formatMoney(g.totals.my_share, g.currency)}
        </p>
        <ul className="mt-3 space-y-1.5" aria-label="Group balances">
          {g.balances.map((b) => (
            <li key={b.member} className="flex items-baseline justify-between gap-2 text-[14px]">
              <span className="truncate text-ink-200">{names[b.member]}</span>
              <span className={cx('tabular-nums', b.net > 0.004 ? 'text-signal' : b.net < -0.004 ? 'text-danger-400' : 'text-ink-500')}>
                {b.net > 0.004 ? '+' : ''}
                {formatMoney(b.net, g.currency)}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {pairs.length > 0 && (
        <section className="rise" style={riseStyle(1)}>
          <div className="flex items-center justify-between px-1 pb-2">
            <h3 className="font-display text-[16px] font-semibold text-ink-100">You and each person</h3>
            <button type="button" onClick={() => go('more', 'money-debts')} className="press flex min-h-[44px] items-center gap-1 text-[13px] text-signal">
              Debts
              <Icon name="right" size={14} />
            </button>
          </div>
          <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {pairs.map((p, i) => (
              <PairRow key={p.member} pair={p} currency={g.currency} i={i} onSettle={() => setSheet({ id: next.current++, open: true, kind: 'settle', entry: null, pair: p })} />
            ))}
          </ul>
          <p className="px-1 pt-1 text-[12px] text-ink-500">Same lines as the Debts page: a repayment recorded on either page shows on both.</p>
        </section>
      )}

      {others.length > 0 && (
        <section className="rise" style={riseStyle(2)}>
          <h3 className="px-1 pb-2 font-display text-[16px] font-semibold text-ink-100">Between the others</h3>
          <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {others.map((s) => {
              const label = `${names[s.from]} pays ${names[s.to]}`;
              return (
                <button key={`${s.from}-${s.to}`} type="button" onClick={() => void settleOthers(s.from, s.to, s.amount, label)} className="press flex min-h-[56px] w-full items-center gap-3 px-4 text-left hover:bg-ink-850">
                  <Icon name="right" size={16} className="text-ink-500" />
                  <span className="min-w-0 flex-1 truncate text-[15px] text-ink-100">{label}</span>
                  <span className="font-display text-[16px] font-semibold tabular-nums text-ink-100">{formatMoney(s.amount, g.currency)}</span>
                </button>
              );
            })}
          </div>
          <p className="px-1 pt-1 text-[12px] text-ink-500">Tap a line once the money changed hands.</p>
        </section>
      )}

      <Button variant="primary" icon="plus" className="w-full" onClick={() => setSheet({ id: next.current++, open: true, kind: 'entry', entry: null })}>
        Add a shared expense
      </Button>

      {!g.entries.length ? (
        <EmptyState icon="receipt" title="No shared expense yet" hint="Add who paid what; balances, who-pays-whom and the Debts page update by themselves." />
      ) : (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {g.entries.map((e, i) => (
            <button key={e.id} type="button" onClick={() => setSheet({ id: next.current++, open: true, kind: 'entry', entry: e })} className="press rise flex min-h-[60px] w-full items-center gap-3 px-4 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full" style={{ background: `${e.kind === 'settlement' ? '#5EE0A8' : CATEGORY_COLOR[e.category] ?? '#8A8FBF'}22`, color: e.kind === 'settlement' ? '#5EE0A8' : CATEGORY_COLOR[e.category] ?? '#8A8FBF' }}>
                <Icon name={e.kind === 'settlement' ? 'repeat' : 'receipt'} size={18} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{e.kind === 'settlement' ? `${names[e.paid_by]} paid ${Object.keys(e.shares)[0] === g.me ? 'you' : names[Object.keys(e.shares)[0]] ?? '?'} back` : e.description || 'Expense'}</span>
                <span className="block truncate text-[12px] text-ink-500">
                  {dayShort(e.date)}
                  {e.kind === 'expense' && ` · paid by ${names[e.paid_by]}`}
                  {e.kind === 'expense' && e.split !== 'equal' ? (e.split === 'percent' ? ' · by %' : ' · custom') : ''}
                  {e.kind === 'expense' && e.shares[g.me] ? ` · your share ${formatMoney(e.shares[g.me], g.currency)}` : ''}
                </span>
              </span>
              <span className="shrink-0 font-display text-[16px] font-semibold tabular-nums text-ink-100">{formatMoney(e.amount, g.currency)}</span>
            </button>
          ))}
        </div>
      )}

      {sheet?.kind === 'entry' && <EntrySheet key={sheet.id} open={sheet.open} group={g} entry={sheet.entry} onClose={close} onSaved={set} />}
      {sheet?.kind === 'group' && <GroupSheet key={sheet.id} open={sheet.open} group={g} onClose={close} onSaved={set} />}
      {sheet?.kind === 'settle' && sheet.pair && <SettleSheet key={sheet.id} open={sheet.open} group={g} pair={sheet.pair} onClose={close} onSaved={set} />}
    </div>
  );
}

// ------------------------------------------------------------------ groups

/** Money › Split: Tricount-like groups. Members are just names. */
export default function SplitView() {
  const [current, setCurrent] = useState<string | null>(() => peekSplitGroup());
  useEffect(() => {
    clearSplitGroup();
  }, []);
  const [sheet, setSheet] = useState<{ id: number; open: boolean } | null>(null);
  const next = useRef(1);
  const data = useHubData((c, s) => c.splitGroups(s), [current]);
  useTopic(['money'], data.reload);
  usePullRefresh(data.reload);

  if (current) return <GroupDetail id={current} onBack={() => setCurrent(null)} />;
  const groups: SplitGroupSummary[] = data.data ?? [];
  return (
    <div className="space-y-4">
      <Button variant="primary" icon="plus" className="w-full" onClick={() => setSheet({ id: next.current++, open: true })}>
        New group
      </Button>
      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
      {data.loading && !data.data && <SkeletonList rows={3} />}
      {data.data && !groups.length && (
        <EmptyState icon="cash" title="No group yet" hint="A trip, a flat share, a dinner: add who paid what, see who owes whom on the Debts page, settle in a few payments." />
      )}
      {groups.length > 0 && (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {groups.map((g, i) => (
            <button key={g.id} type="button" onClick={() => setCurrent(g.id)} className="press rise flex min-h-[64px] w-full items-center gap-3 px-4 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-signal/15 font-display text-[15px] font-semibold text-signal">{g.name.slice(0, 1).toUpperCase()}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{g.name}</span>
                <span className="block truncate text-[12px] text-ink-500">
                  {g.members.length} people · {g.totals.count} expense{g.totals.count === 1 ? '' : 's'} · {formatMoney(g.totals.spent, g.currency)}
                </span>
              </span>
              <span className={cx('shrink-0 text-right text-[13px]', g.totals.my_net > 0 ? 'text-signal' : g.totals.my_net < 0 ? 'text-danger-400' : 'text-ink-500')}>{myBalanceLabel(g.totals.my_net, g.currency)}</span>
            </button>
          ))}
        </div>
      )}
      {sheet && (
        <GroupSheet
          key={sheet.id}
          open={sheet.open}
          group={null}
          onClose={() => setSheet((s) => (s ? { ...s, open: false } : s))}
          onSaved={(g) => {
            data.reload();
            if (g) setCurrent(g.id);
          }}
        />
      )}
    </div>
  );
}
