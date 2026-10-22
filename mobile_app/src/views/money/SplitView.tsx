import { useMemo, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, EmptyState, ErrorNote, Field, IconButton, Segmented, SkeletonList, cx, inputClass, riseStyle } from '../../components/ui';
import { formatMoney, isoDay } from '../../lib/apps';
import { announce, useTopic } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { parseAmount } from '../../lib/money';
import { customSharesError, equalShares, memberNames, myBalanceLabel } from '../../lib/moneyTools';
import type { SplitEntry, SplitGroup, SplitGroupSummary } from '../../lib/moneyTypes';
import { usePullRefresh } from '../../lib/nav';
import { EXPENSE_CATEGORIES, type ExpenseCategory } from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';

function dayShort(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
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
    if (!(await confirm({ title: `Delete ${group.name}?`, body: 'Its shared expenses go too, and your shares leave your expenses.', confirmLabel: 'Delete group', tone: 'danger' }))) return;
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

function EntrySheet({ open, onClose, group, entry, onSaved }: { open: boolean; onClose: () => void; group: SplitGroup; entry: SplitEntry | null; onSaved: (g: SplitGroup) => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const names = memberNames(group.members);
  const ids = group.members.map((m) => m.id);
  const [description, setDescription] = useState(entry?.description ?? '');
  const [amount, setAmount] = useState(entry ? String(entry.amount) : '');
  const [paidBy, setPaidBy] = useState(entry?.paid_by ?? group.me);
  const [date, setDate] = useState(entry?.date ?? isoDay());
  const [category, setCategory] = useState<ExpenseCategory>(entry?.category ?? 'food');
  const [mode, setMode] = useState<'equal' | 'custom'>(entry?.split ?? 'equal');
  const [who, setWho] = useState<string[]>(entry && entry.split === 'equal' ? Object.keys(entry.shares) : ids);
  const [custom, setCustom] = useState<Record<string, string>>(() =>
    Object.fromEntries(ids.map((id) => [id, entry && entry.split === 'custom' && entry.shares[id] ? String(entry.shares[id]) : ''])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const settlement = entry?.kind === 'settlement';

  const value = parseAmount(amount) ?? 0;
  const preview = useMemo(() => equalShares(value, ids.filter((id) => who.includes(id))), [value, who, ids]);
  const customNums = Object.fromEntries(Object.entries(custom).map(([id, v]) => [id, parseAmount(v) ?? 0]));
  const customError = mode === 'custom' ? customSharesError(value, customNums) : null;

  const save = async () => {
    if (value <= 0) return setError('Enter the amount.');
    if (mode === 'equal' && !who.length) return setError('Pick who shares it.');
    if (customError) return setError(customError);
    setBusy(true);
    setError(null);
    const body = {
      description: description.trim(),
      amount: value,
      paid_by: paidBy,
      date,
      category,
      split: mode,
      ...(mode === 'equal' ? { participants: ids.filter((id) => who.includes(id)) } : { shares: Object.fromEntries(Object.entries(customNums).filter(([, v]) => v > 0)) }),
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
    if (!(await confirm({ title: settlement ? 'Delete this repayment?' : `Delete ${entry.description || 'this expense'}?`, confirmLabel: 'Delete', tone: 'danger' }))) return;
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
        {error && <ErrorNote message={error} />}
        <p className="text-[15px] text-ink-200">
          {names[entry.paid_by]} paid {names[Object.keys(entry.shares)[0]] ?? '?'} back {formatMoney(entry.amount, group.currency)} on {dayShort(entry.date)}.
        </p>
      </Sheet>
    );
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={entry ? 'Edit shared expense' : 'Shared expense'}
      headerAction={entry ? <IconButton icon="trash" label="Delete shared expense" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={() => void save()} disabled={busy}>
          {busy ? 'Saving…' : entry ? 'Save' : 'Add expense'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <div className="flex items-end gap-2">
          <Field label={`Amount (${group.currency})`}>
            <input inputMode="decimal" value={amount} autoFocus={!entry} onChange={(e) => setAmount(e.target.value.replace(/[^\d.,\s]/g, ''))} placeholder="0.00" className={cx(inputClass, 'font-display text-[28px] font-semibold')} />
          </Field>
        </div>
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
        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { value: 'equal', label: 'Split equally' },
            { value: 'custom', label: 'Custom amounts' },
          ]}
        />
        {mode === 'equal' ? (
          <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850 ring-1 ring-inset ring-ink-700">
            {group.members.map((m) => {
              const on = who.includes(m.id);
              return (
                <li key={m.id}>
                  <button type="button" aria-pressed={on} onClick={() => setWho((w) => (on ? w.filter((x) => x !== m.id) : [...w, m.id]))} className="press flex min-h-[48px] w-full items-center gap-3 px-3 text-left">
                    <Icon name={on ? 'check' : 'circle'} size={18} className={on ? 'text-signal' : 'text-ink-500'} />
                    <span className="min-w-0 flex-1 truncate text-[15px] text-ink-100">{names[m.id]}</span>
                    <span className="tabular-nums text-[14px] text-ink-400">{on ? formatMoney(preview[m.id] ?? 0, group.currency) : '-'}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="space-y-2">
            {group.members.map((m) => (
              <label key={m.id} className="flex items-center gap-3">
                <span className="min-w-0 flex-1 truncate text-[15px] text-ink-200">{names[m.id]}</span>
                <input inputMode="decimal" value={custom[m.id] ?? ''} onChange={(e) => setCustom((c) => ({ ...c, [m.id]: e.target.value.replace(/[^\d.,\s]/g, '') }))} placeholder="0" className={cx(inputClass, 'w-32 text-right')} />
              </label>
            ))}
            <p className={cx('px-1 text-[13px]', customError ? 'text-amber-400' : 'text-mint-400')}>{customError ?? 'Adds up ✓'}</p>
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

// ------------------------------------------------------------------ one group

function GroupDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const data = useHubData((c, s) => c.splitGroup(id, s), [id]);
  usePullRefresh(data.reload);
  const [sheet, setSheet] = useState<{ id: number; open: boolean; kind: 'entry' | 'group'; entry: SplitEntry | null } | null>(null);
  const next = useRef(1);
  const g = data.data;
  const set = (fresh: SplitGroup | null) => (fresh ? data.setData(() => fresh) : onBack());
  const close = () => setSheet((s) => (s ? { ...s, open: false } : s));

  const settle = async (from: string, to: string, amount: number, label: string) => {
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
        {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
        {data.loading && <SkeletonList rows={3} />}
      </div>
    );
  }
  const names = memberNames(g.members);
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-1">
        <IconButton icon="left" label="All groups" onClick={onBack} />
        <h2 className="min-w-0 flex-1 truncate font-display text-[22px] font-bold text-ink-100">{g.name}</h2>
        <IconButton icon="edit" label="Edit group" onClick={() => setSheet({ id: next.current++, open: true, kind: 'group', entry: null })} />
      </div>

      <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
        <p className={cx('font-display text-[22px] font-semibold', g.totals.my_net > 0 ? 'text-signal' : g.totals.my_net < 0 ? 'text-danger-400' : 'text-mint-400')}>
          {myBalanceLabel(g.totals.my_net, g.currency)}
        </p>
        <p className="pt-1 text-[13px] text-ink-400">
          Group spent {formatMoney(g.totals.spent, g.currency)} · your share {formatMoney(g.totals.my_share, g.currency)}
        </p>
        <ul className="mt-3 space-y-1.5">
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

      {g.settle.length > 0 && (
        <section className="rise" style={riseStyle(1)}>
          <h3 className="px-1 pb-2 font-display text-[16px] font-semibold text-ink-100">Settle up</h3>
          <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {g.settle.map((s) => {
              const label = `${names[s.from]} ${s.from === g.me ? 'pay' : 'pays'} ${names[s.to]}`;
              return (
                <button key={`${s.from}-${s.to}`} type="button" onClick={() => void settle(s.from, s.to, s.amount, label)} className="press flex min-h-[56px] w-full items-center gap-3 px-4 text-left hover:bg-ink-850">
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
        <EmptyState icon="receipt" title="No shared expense yet" hint="Add who paid what; balances and who-pays-whom update by themselves." />
      ) : (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {g.entries.map((e, i) => (
            <button key={e.id} type="button" onClick={() => setSheet({ id: next.current++, open: true, kind: 'entry', entry: e })} className="press rise flex min-h-[60px] w-full items-center gap-3 px-4 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full" style={{ background: `${e.kind === 'settlement' ? '#5EE0A8' : CATEGORY_COLOR[e.category] ?? '#8A8FBF'}22`, color: e.kind === 'settlement' ? '#5EE0A8' : CATEGORY_COLOR[e.category] ?? '#8A8FBF' }}>
                <Icon name={e.kind === 'settlement' ? 'repeat' : 'receipt'} size={18} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{e.kind === 'settlement' ? `${names[e.paid_by]} paid ${names[Object.keys(e.shares)[0]] ?? '?'} back` : e.description || 'Expense'}</span>
                <span className="block truncate text-[12px] text-ink-500">
                  {dayShort(e.date)}
                  {e.kind === 'expense' && ` · paid by ${names[e.paid_by]}`}
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
    </div>
  );
}

// ------------------------------------------------------------------ groups

/** Money › Split: Tricount-like groups. Members are just names. */
export default function SplitView() {
  const [current, setCurrent] = useState<string | null>(null);
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
        <EmptyState icon="cash" title="No group yet" hint="A trip, a flat share, a dinner: add who paid what, see who owes whom, settle in a few payments." />
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
