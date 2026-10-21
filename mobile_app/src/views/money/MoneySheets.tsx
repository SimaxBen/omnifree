import { useState } from 'react';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Button, Chip, ErrorNote, Field, IconButton, Toggle, cx, inputClass } from '../../components/ui';
import { currentMonth, isoDay } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import { parseAmount } from '../../lib/money';
import {
  ACCOUNT_KINDS,
  EXPENSE_CATEGORIES,
  INCOME_CATEGORIES,
  type AccountKind,
  type ExpenseCategory,
  type Income,
  type IncomeCategory,
  type MoneyAccount,
  type RecurringExpense,
} from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';

const CURRENCIES = ['MAD', 'EUR', 'USD', 'GBP'];

export const INCOME_LABEL: Record<IncomeCategory, string> = {
  salary: 'Salary',
  freelance: 'Freelance',
  refund: 'Refund',
  gift: 'Gift',
  other: 'Other',
};

const KIND_LABEL: Record<AccountKind, string> = { cash: 'Cash', bank: 'Bank', savings: 'Savings', card: 'Card', other: 'Other' };

/** Big amount input with its currency next to it. */
function AmountRow({
  label = 'Amount',
  amount,
  currency,
  onAmount,
  onCurrency,
  allowNegative,
  autoFocus,
}: {
  label?: string;
  amount: string;
  currency: string;
  onAmount: (v: string) => void;
  onCurrency: (v: string) => void;
  allowNegative?: boolean;
  autoFocus?: boolean;
}) {
  return (
    <div className="flex items-end gap-2">
      <Field label={label}>
        <input
          inputMode="decimal"
          value={amount}
          autoFocus={autoFocus}
          onChange={(e) => onAmount(e.target.value.replace(allowNegative ? /[^\d.,\s-]/g : /[^\d.,\s]/g, ''))}
          placeholder="0.00"
          className={cx(inputClass, 'font-display text-[28px] font-semibold')}
        />
      </Field>
      <label className="w-28 shrink-0">
        <span className="sr-only">Currency</span>
        <input list="money-currencies" value={currency} onChange={(e) => onCurrency(e.target.value.toUpperCase().slice(0, 3))} className={cx(inputClass, 'text-center font-semibold')} />
        <datalist id="money-currencies">
          {CURRENCIES.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
      </label>
    </div>
  );
}

function ExpenseCategoryChips({ value, onChange }: { value: ExpenseCategory; onChange: (c: ExpenseCategory) => void }) {
  return (
    <div className="space-y-1.5">
      <span className="block px-1 text-[13px] text-ink-300">Category</span>
      <div className="flex flex-wrap gap-2">
        {EXPENSE_CATEGORIES.map((c) => (
          <Chip key={c} active={value === c} onClick={() => onChange(c)}>
            <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[c] }} />
            {c}
          </Chip>
        ))}
      </div>
    </div>
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

// ------------------------------------------------------------------ income

export function IncomeSheet({ open, onClose, income }: { open: boolean; onClose: () => void; income: Income | null }) {
  const { client } = useHub();
  const confirm = useConfirm();
  const [amount, setAmount] = useState(income ? String(income.amount) : '');
  const [currency, setCurrency] = useState(income?.currency ?? 'MAD');
  const [source, setSource] = useState(income?.source ?? '');
  const [category, setCategory] = useState<IncomeCategory>(income?.category ?? 'salary');
  const [date, setDate] = useState(income?.date ?? isoDay());
  const [recurring, setRecurring] = useState(income?.recurring ?? false);
  const [endMonth, setEndMonth] = useState(income?.end_month ?? '');
  const [note, setNote] = useState(income?.note ?? '');
  const { saving, error, setError, run } = useSave(onClose);

  const save = () => {
    const value = parseAmount(amount);
    if (value === null || value <= 0) {
      setError('Enter the amount you received.');
      return;
    }
    const fields = {
      amount: value,
      currency: currency.trim().toUpperCase() || 'MAD',
      source: source.trim(),
      category,
      date,
      recurring,
      end_month: recurring && endMonth ? endMonth : null,
      note: note.trim() || null,
    };
    void run(() => (income ? client.updateIncome(income.id, fields) : client.createIncome(fields)), income ? 'Income updated' : 'Income added');
  };

  const remove = async () => {
    if (!income) return;
    if (!(await confirm({ title: 'Delete this income?', body: `${income.source || INCOME_LABEL[income.category]}, ${income.amount} ${income.currency}`, confirmLabel: 'Delete income', tone: 'danger' }))) return;
    void run(() => client.deleteIncome(income.id), 'Income deleted');
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={income ? 'Edit income' : 'New income'}
      headerAction={income ? <IconButton icon="trash" label="Delete income" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : income ? 'Save income' : 'Add income'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <AmountRow amount={amount} currency={currency} onAmount={setAmount} onCurrency={setCurrency} autoFocus={!income} />
        <div className="flex flex-wrap gap-2">
          {INCOME_CATEGORIES.map((c) => (
            <Chip key={c} active={category === c} onClick={() => setCategory(c)}>
              {INCOME_LABEL[c]}
            </Chip>
          ))}
        </div>
        <Field label="From">
          <input value={source} onChange={(e) => setSource(e.target.value)} placeholder="Salaire, client name…" className={inputClass} />
        </Field>
        <Field label={recurring ? 'First pay day' : 'Date'}>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className={inputClass} />
        </Field>
        <div className="flex min-h-[52px] items-center justify-between gap-3 rounded-row bg-ink-850 px-4 ring-1 ring-inset ring-ink-700">
          <span className="min-w-0">
            <span className="block text-[15px] text-ink-100">Every month</span>
            <span className="block text-[12px] text-ink-500">Counts each month from this date</span>
          </span>
          <Toggle checked={recurring} onChange={setRecurring} label="Monthly income" />
        </div>
        {recurring && (
          <Field label="Last month (optional)" hint="Leave empty while it keeps coming.">
            <input type="month" value={endMonth} min={date.slice(0, 7)} onChange={(e) => setEndMonth(e.target.value)} className={inputClass} />
          </Field>
        )}
        <Field label="Note">
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" className={inputClass} />
        </Field>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ recurring expense

export function RecurringSheet({ open, onClose, item }: { open: boolean; onClose: () => void; item: RecurringExpense | null }) {
  const { client } = useHub();
  const confirm = useConfirm();
  const [name, setName] = useState(item?.name ?? '');
  const [amount, setAmount] = useState(item ? String(item.amount) : '');
  const [currency, setCurrency] = useState(item?.currency ?? 'MAD');
  const [category, setCategory] = useState<ExpenseCategory>(item?.category ?? 'bills');
  const [day, setDay] = useState(String(item?.day ?? 1));
  const [start, setStart] = useState(item?.start_month ?? currentMonth());
  const [end, setEnd] = useState(item?.end_month ?? '');
  const { saving, error, setError, run } = useSave(onClose);

  const save = () => {
    const value = parseAmount(amount);
    if (!name.trim()) {
      setError('Give it a name (Rent, Claude Max, Internet…).');
      return;
    }
    if (value === null || value <= 0) {
      setError('Enter the monthly amount.');
      return;
    }
    const fields = {
      name: name.trim(),
      amount: value,
      currency: currency.trim().toUpperCase() || 'MAD',
      category,
      day: Math.min(31, Math.max(1, Math.round(Number(day) || 1))),
      start_month: start || currentMonth(),
      end_month: end || null,
    };
    void run(() => (item ? client.updateRecurring(item.id, fields) : client.createRecurring(fields)), item ? 'Recurring expense updated' : 'Recurring expense added');
  };

  const remove = async () => {
    if (!item) return;
    if (!(await confirm({ title: `Stop tracking ${item.name}?`, body: 'It disappears from every month, past ones included. To keep the history, set a last month instead.', confirmLabel: 'Delete', tone: 'danger' }))) return;
    void run(() => client.deleteRecurring(item.id), 'Recurring expense deleted');
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={item ? 'Edit recurring' : 'New recurring expense'}
      headerAction={item ? <IconButton icon="trash" label="Delete recurring expense" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : item ? 'Save' : 'Add recurring expense'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Rent, Claude Max, Internet…" className={inputClass} autoFocus={!item} />
        </Field>
        <AmountRow label="Every month" amount={amount} currency={currency} onAmount={setAmount} onCurrency={setCurrency} />
        <ExpenseCategoryChips value={category} onChange={setCategory} />
        <div className="grid grid-cols-[88px_1fr] gap-2">
          <Field label="Day">
            <input inputMode="numeric" value={day} onChange={(e) => setDay(e.target.value.replace(/\D/g, '').slice(0, 2))} className={cx(inputClass, 'text-center')} />
          </Field>
          <Field label="From">
            <input type="month" value={start} onChange={(e) => setStart(e.target.value)} className={inputClass} />
          </Field>
        </div>
        <Field label="Until (optional)" hint="Set it when you cancel the subscription: past months keep counting it.">
          <input type="month" value={end} min={start} onChange={(e) => setEnd(e.target.value)} className={inputClass} />
        </Field>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ budget

export function BudgetSheet({
  open,
  onClose,
  category: initial,
  current,
}: {
  open: boolean;
  onClose: () => void;
  category: ExpenseCategory | null;
  current: { amount: number; currency: string } | null;
}) {
  const { client } = useHub();
  const [category, setCategory] = useState<ExpenseCategory>(initial ?? 'food');
  const [amount, setAmount] = useState(current ? String(current.amount) : '');
  const [currency, setCurrency] = useState(current?.currency ?? 'MAD');
  const { saving, error, setError, run } = useSave(onClose);

  const save = () => {
    const value = parseAmount(amount);
    if (value === null || value <= 0) {
      setError('Enter a monthly limit.');
      return;
    }
    void run(() => client.setBudget(category, value, currency.trim().toUpperCase() || 'MAD'), `Budget set for ${category}`);
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={initial ? `Budget: ${initial}` : 'Set a budget'}
      footer={
        <div className="flex gap-2">
          {initial && current && (
            <Button variant="danger" onClick={() => void run(() => client.deleteBudget(initial), 'Budget removed')} disabled={saving}>
              Remove
            </Button>
          )}
          <Button variant="primary" className="flex-1" onClick={save} disabled={saving}>
            {saving ? 'Saving…' : 'Save budget'}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        {!initial && <ExpenseCategoryChips value={category} onChange={setCategory} />}
        <AmountRow label="Monthly limit" amount={amount} currency={currency} onAmount={setAmount} onCurrency={setCurrency} autoFocus={!!initial} />
        <p className="px-1 text-[13px] text-ink-400">Receipts, email imports, voice entries and recurring expenses in this category all count.</p>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ account

export function AccountSheet({ open, onClose, account }: { open: boolean; onClose: () => void; account: MoneyAccount | null }) {
  const { client } = useHub();
  const confirm = useConfirm();
  const [name, setName] = useState(account?.name ?? '');
  const [kind, setKind] = useState<AccountKind>(account?.kind ?? 'bank');
  const [balance, setBalance] = useState(account ? String(account.balance) : '');
  const [currency, setCurrency] = useState(account?.currency ?? 'MAD');
  const { saving, error, setError, run } = useSave(onClose);

  const save = () => {
    const value = parseAmount(balance);
    if (!name.trim()) {
      setError('Name the account (Cash, CIH, Wise…).');
      return;
    }
    if (value === null) {
      setError('Enter the current balance.');
      return;
    }
    const fields = { name: name.trim(), kind, balance: value, currency: currency.trim().toUpperCase() || 'MAD' };
    void run(() => (account ? client.updateAccount(account.id, fields) : client.createAccount(fields)), account ? 'Balance updated' : 'Account added');
  };

  const remove = async () => {
    if (!account) return;
    if (!(await confirm({ title: `Delete ${account.name}?`, confirmLabel: 'Delete account', tone: 'danger' }))) return;
    void run(() => client.deleteAccount(account.id), 'Account deleted');
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={account ? account.name : 'New account'}
      headerAction={account ? <IconButton icon="trash" label="Delete account" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : account ? 'Save balance' : 'Add account'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <AmountRow label="Balance today" amount={balance} currency={currency} onAmount={setBalance} onCurrency={setCurrency} allowNegative autoFocus={!!account} />
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Cash, CIH, Wise…" className={inputClass} autoFocus={!account} />
        </Field>
        <div className="flex flex-wrap gap-2">
          {ACCOUNT_KINDS.map((k) => (
            <Chip key={k} active={kind === k} onClick={() => setKind(k)}>
              {KIND_LABEL[k]}
            </Chip>
          ))}
        </div>
      </div>
    </Sheet>
  );
}
