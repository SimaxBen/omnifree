import { useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, ErrorNote, Field, IconButton, Segmented, cx, inputClass, riseStyle } from '../../components/ui';
import { formatMoney, isoDay } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import { parseAmount } from '../../lib/money';
import { debtDetail, debtHeadline } from '../../lib/moneyTools';
import type { Debt, DebtDirection, DebtListing } from '../../lib/moneyTypes';

const CURRENCIES = ['MAD', 'EUR', 'USD', 'GBP'];

function AmountInput({ amount, currency, onAmount, onCurrency, label = 'Amount', autoFocus }: {
  amount: string;
  currency: string;
  onAmount: (v: string) => void;
  onCurrency: (v: string) => void;
  label?: string;
  autoFocus?: boolean;
}) {
  return (
    <div className="flex items-end gap-2">
      <Field label={label}>
        <input
          inputMode="decimal"
          value={amount}
          autoFocus={autoFocus}
          onChange={(e) => onAmount(e.target.value.replace(/[^\d.,\s]/g, ''))}
          placeholder="0.00"
          className={cx(inputClass, 'font-display text-[28px] font-semibold')}
        />
      </Field>
      <label className="w-28 shrink-0">
        <span className="sr-only">Currency</span>
        <input list="debt-currencies" value={currency} onChange={(e) => onCurrency(e.target.value.toUpperCase().slice(0, 3))} className={cx(inputClass, 'text-center font-semibold')} />
        <datalist id="debt-currencies">
          {CURRENCIES.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
      </label>
    </div>
  );
}

/** New loan, or an existing one: repayments, edit, settle, delete. */
export function DebtSheet({ open, onClose, debt: initial, direction: initialDirection }: { open: boolean; onClose: () => void; debt: Debt | null; direction?: DebtDirection }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [debt, setDebt] = useState<Debt | null>(initial);
  const [direction, setDirection] = useState<DebtDirection>(initial?.direction ?? initialDirection ?? 'lent');
  const [person, setPerson] = useState(initial?.person ?? '');
  const [amount, setAmount] = useState(initial ? String(initial.amount) : '');
  const [currency, setCurrency] = useState(initial?.currency ?? 'MAD');
  const [date, setDate] = useState(initial?.date ?? isoDay());
  const [due, setDue] = useState(initial?.due_date ?? '');
  const [note, setNote] = useState(initial?.note ?? '');
  const [repay, setRepay] = useState(initial && !initial.settled ? String(initial.remaining) : '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<Debt | void>, done: string, close = false) => {
    setBusy(true);
    setError(null);
    try {
      const out = await fn();
      if (out) {
        setDebt(out);
        setRepay(out.settled ? '' : String(out.remaining));
      }
      announce('money');
      notify(done, 'ok');
      if (close) onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const value = parseAmount(amount);
    if (!person.trim()) return setError('Who is it with?');
    if (value === null || value <= 0) return setError('Enter the amount.');
    const fields = { person: person.trim(), direction, amount: value, currency: currency.trim().toUpperCase() || 'MAD', date, due_date: due || null, note: note.trim() || null };
    void act(() => (debt ? client.updateDebt(debt.id, fields) : client.createDebt(fields)), debt ? 'Saved' : 'Loan added', true);
  };

  const recordRepayment = () => {
    if (!debt) return;
    const value = parseAmount(repay);
    if (value === null || value <= 0) return setError('Enter the amount paid back.');
    void act(() => client.repayDebt(debt.id, { amount: value }), value >= debt.remaining ? 'Settled ✓' : 'Repayment recorded');
  };

  const remove = async () => {
    if (!debt) return;
    if (!(await confirm({ title: `Delete this loan with ${debt.person}?`, body: 'Its repayments are deleted too.', confirmLabel: 'Delete loan', tone: 'danger' }))) return;
    void act(() => client.deleteDebt(debt.id), 'Loan deleted', true);
  };

  const today = isoDay();
  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={debt ? debtHeadline(debt) : 'New loan'}
      headerAction={debt ? <IconButton icon="trash" label="Delete loan" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={busy}>
          {busy ? 'Saving…' : debt ? 'Save changes' : 'Add loan'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        {debt && (
          <section className="space-y-3 rounded-card bg-ink-850 p-4 ring-1 ring-inset ring-ink-700">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[13px] text-ink-400">{debt.settled ? 'Settled' : 'Left to repay'}</span>
              <span className={cx('font-display text-[24px] font-semibold tabular-nums', debt.settled ? 'text-mint-400' : debt.direction === 'lent' ? 'text-signal' : 'text-danger-400')}>
                {formatMoney(debt.settled ? debt.amount : debt.remaining, debt.currency)}
              </span>
            </div>
            <p className="text-[12px] text-ink-500">{debtDetail(debt, today)}</p>
            {!debt.settled && (
              <div className="flex items-end gap-2">
                <Field label={debt.direction === 'lent' ? `${debt.person} paid back` : `You paid ${debt.person} back`}>
                  <input inputMode="decimal" value={repay} onChange={(e) => setRepay(e.target.value.replace(/[^\d.,\s]/g, ''))} className={inputClass} />
                </Field>
                <Button variant="primary" icon="check" onClick={recordRepayment} disabled={busy} className="shrink-0">
                  Record
                </Button>
              </div>
            )}
            {debt.payments.length > 0 && (
              <ul className="divide-y divide-ink-800">
                {debt.payments.map((p) => (
                  <li key={p.id} className="flex min-h-[44px] items-center gap-2 text-[14px]">
                    <Icon name="undo" size={14} className="text-ink-500" />
                    <span className="min-w-0 flex-1 text-ink-300">
                      {p.date}
                      {p.origin === 'voice' ? ' · voice' : ''}
                    </span>
                    <span className="tabular-nums text-ink-100">{formatMoney(p.amount, debt.currency)}</span>
                    <IconButton icon="x" label="Remove repayment" disabled={busy} onClick={() => void act(() => client.deleteDebtPayment(debt.id, p.id), 'Repayment removed')} />
                  </li>
                ))}
              </ul>
            )}
            {/* A loan closed by its repayments has nothing left: it opens again by removing a repayment above. */}
            {!(debt.settled && debt.paid >= debt.amount - 0.005) && (
              <Button variant="ghost" className="w-full" onClick={() => void act(() => client.updateDebt(debt.id, { settled: !debt.settled }), debt.settled ? 'Opened again' : 'Marked as settled')} disabled={busy}>
                {debt.settled ? 'Open it again' : 'Mark as settled (forget the rest)'}
              </Button>
            )}
          </section>
        )}
        <Segmented
          value={direction}
          onChange={setDirection}
          options={[
            { value: 'lent', label: 'They owe me' },
            { value: 'borrowed', label: 'I owe them' },
          ]}
        />
        <Field label={direction === 'lent' ? 'Lent to' : 'Borrowed from'}>
          <input value={person} onChange={(e) => setPerson(e.target.value)} placeholder="Ayoub, Karim…" className={inputClass} autoFocus={!debt} />
        </Field>
        <AmountInput amount={amount} currency={currency} onAmount={setAmount} onCurrency={setCurrency} />
        <div className="grid grid-cols-2 gap-2">
          <Field label="Date">
            <input type="date" value={date} max={today} onChange={(e) => setDate(e.target.value)} className={inputClass} />
          </Field>
          <Field label="Due (optional)">
            <input type="date" value={due} min={date} onChange={(e) => setDue(e.target.value)} className={inputClass} />
          </Field>
        </div>
        <Field label="Note">
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" className={inputClass} />
        </Field>
      </div>
    </Sheet>
  );
}

/** One loan as a row (Money page and the Debts page). */
export function DebtRow({ debt, onClick, i }: { debt: Debt; onClick: () => void; i: number }) {
  const lent = debt.direction === 'lent';
  return (
    <button type="button" onClick={onClick} className="press rise flex min-h-[60px] w-full items-center gap-3 px-4 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
      <span className={cx('grid h-10 w-10 shrink-0 place-items-center rounded-full', lent ? 'bg-signal/15 text-signal' : 'bg-danger-400/15 text-danger-400')}>
        <Icon name={lent ? 'up' : 'down'} size={18} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[15px] text-ink-100">{debtHeadline(debt)}</span>
          {debt.overdue && <Badge tone="bad">late</Badge>}
        </span>
        <span className="block truncate text-[12px] text-ink-500">{debtDetail(debt, isoDay())}</span>
      </span>
      <span className={cx('shrink-0 font-display text-[16px] font-semibold tabular-nums', debt.settled ? 'text-ink-500 line-through' : lent ? 'text-signal' : 'text-danger-400')}>
        {formatMoney(debt.settled ? debt.amount : debt.remaining, debt.currency)}
      </span>
    </button>
  );
}

/** The "Owed" totals line: what people owe me, what I owe. */
export function OwedTotals({ data }: { data: DebtListing }) {
  const s = data.summary;
  return (
    <div className="grid grid-cols-2 gap-2">
      <div className="rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
        <p className="text-[12px] text-ink-400">Owed to you</p>
        <p className="font-display text-[18px] font-semibold tabular-nums text-signal">{formatMoney(s.owed_to_me, s.currency)}</p>
      </div>
      <div className="rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
        <p className="text-[12px] text-ink-400">You owe</p>
        <p className={cx('font-display text-[18px] font-semibold tabular-nums', s.i_owe > 0 ? 'text-danger-400' : 'text-ink-100')}>{formatMoney(s.i_owe, s.currency)}</p>
      </div>
    </div>
  );
}
