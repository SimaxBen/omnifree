import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, ErrorNote, Field, IconButton, cx, haptic, inputClass } from '../../components/ui';
import { isoDay } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { emailMailbox } from '../../lib/digest';
import { errorText, useHub } from '../../lib/hub';
import { EXPENSE_CATEGORIES, type Expense, type ExpenseCategory, type ExpenseCreate, type ExpenseItem } from '../../lib/types';
import { useRuleOffer } from './ruleOffer';

export type ExpenseMode = { kind: 'receipt'; file: File } | { kind: 'new' } | { kind: 'edit'; expense: Expense };

export const CATEGORY_COLOR: Record<ExpenseCategory, string> = {
  food: '#FFC24B',
  groceries: '#5EE0A8',
  transport: '#7FA8FF',
  housing: '#C59BFF',
  bills: '#FF9548',
  health: '#FF7FA8',
  shopping: '#5FD0E8',
  entertainment: '#F5E06B',
  education: '#9FD16A',
  travel: '#FFAE7A',
  other: '#8A8FBF',
};

const CURRENCIES = ['MAD', 'EUR', 'USD', 'GBP'];

interface Form {
  date: string;
  merchant: string;
  total: string;
  currency: string;
  category: ExpenseCategory;
  note: string;
  items: ExpenseItem[];
}

function formOf(e?: Expense | null): Form {
  return {
    date: e?.date ?? isoDay(),
    merchant: e?.merchant ?? '',
    total: e ? String(e.total) : '',
    currency: e?.currency ?? 'MAD',
    category: e?.category ?? 'food',
    note: e?.note ?? '',
    items: e?.items ?? [],
  };
}

function toFields(f: Form): ExpenseCreate {
  return {
    date: f.date,
    merchant: f.merchant.trim(),
    total: Number(f.total.replace(',', '.')) || 0,
    currency: f.currency.trim().toUpperCase() || 'MAD',
    category: f.category,
    note: f.note.trim() || null,
    items: f.items.filter((i) => i.name.trim()),
    receipt_key: null,
  };
}

const CONFIDENCE = {
  high: { tone: 'ok', text: 'Read clearly' },
  medium: { tone: 'warn', text: 'Check the numbers' },
  low: { tone: 'bad', text: 'Hard to read: saved as a draft, please fill it in' },
} as const;

export default function ExpenseEditor({ open, mode, onClose }: { open: boolean; mode: ExpenseMode; onClose: () => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const offerRule = useRuleOffer();
  const [expense, setExpense] = useState<Expense | null>(mode.kind === 'edit' ? mode.expense : null);
  const [form, setForm] = useState<Form>(() => formOf(mode.kind === 'edit' ? mode.expense : null));
  const [scanning, setScanning] = useState(mode.kind === 'receipt');
  const [confidence, setConfidence] = useState<keyof typeof CONFIDENCE | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [photo, setPhoto] = useState<string | null>(null);
  const [showItems, setShowItems] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    if (mode.kind !== 'receipt') return undefined;
    const url = URL.createObjectURL(mode.file);
    setPhoto(url);
    return () => URL.revokeObjectURL(url);
  }, [mode]);

  useEffect(() => {
    if (mode.kind !== 'receipt' || started.current) return;
    started.current = true;
    (async () => {
      try {
        const res = await client.scanReceipt(mode.file, { filename: mode.file.name || 'receipt.jpg' });
        setExpense(res.expense);
        setForm(formOf(res.expense));
        setConfidence(res.confidence);
        haptic([10, 40, 10]);
        announce('expenses');
      } catch (err) {
        setError(errorText(err));
      } finally {
        setScanning(false);
      }
    })();
  }, [client, mode]);

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  const save = async () => {
    const fields = toFields(form);
    if (!fields.merchant && !fields.total) {
      setError('Add a merchant or an amount.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (expense) {
        const { receipt_key: _r, ...patch } = fields;
        await client.updateExpense(expense.id, patch);
      } else {
        await client.createExpense(fields);
      }
      announce('expenses');
      notify(expense ? 'Expense updated' : 'Expense added', 'ok');
      onClose();
      // A new category or name for a known merchant: offer to make it a rule (and to fix the past ones).
      if (expense) void offerRule({ merchant: expense.merchant, category: expense.category }, { merchant: fields.merchant, category: fields.category });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!expense) return;
    if (!(await confirm({ title: 'Delete this expense?', body: `${expense.merchant || 'Untitled'}, ${expense.total} ${expense.currency}`, confirmLabel: 'Delete expense', tone: 'danger' }))) return;
    try {
      await client.deleteExpense(expense.id);
      announce('expenses');
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const title = mode.kind === 'receipt' ? (scanning ? 'Reading receipt' : 'Check receipt') : expense ? 'Edit expense' : 'New expense';

  return (
    <Sheet
      open={open}
      onClose={onClose}
      full={mode.kind === 'receipt'}
      title={title}
      headerAction={expense ? <IconButton icon="trash" label="Delete expense" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        !scanning && (
          <Button variant="primary" className="w-full" onClick={() => void save()} disabled={saving}>
            {saving ? 'Saving…' : expense ? 'Save expense' : 'Add expense'}
          </Button>
        )
      }
    >
      {photo && (
        <div className={cx('relative mx-auto mb-4 overflow-hidden rounded-card bg-ink-950 transition-[height] duration-slow ease-out', scanning ? 'h-[52dvh]' : 'h-28')}>
          {mode.kind === 'receipt' && mode.file.type === 'application/pdf' ? (
            <div className="grid h-full w-full place-items-center text-ink-300">PDF receipt · {mode.file.name}</div>
          ) : (
            <img src={photo} alt="Receipt" className="h-full w-full object-contain" />
          )}
          {scanning && (
            <>
              <div className="absolute inset-0 bg-ink-950/30" />
              <div className="receipt-scan absolute inset-x-0 h-16 bg-gradient-to-b from-transparent via-signal/30 to-transparent" />
              <div className="receipt-scan absolute inset-x-0 h-0.5 translate-y-8 bg-signal shadow-[0_0_16px_#FFC24B]" />
            </>
          )}
        </div>
      )}
      {scanning ? (
        <p className="text-center text-[15px] text-ink-300">Finding the merchant, total and items…</p>
      ) : (
        <div className="space-y-4">
          {confidence && (
            <Badge tone={CONFIDENCE[confidence].tone} className="screen-in-up">
              {CONFIDENCE[confidence].text}
            </Badge>
          )}
          {error && <ErrorNote message={error} />}

          <div className="flex items-end gap-2">
            <Field label="Amount">
              <input
                inputMode="decimal"
                value={form.total}
                onChange={(e) => set('total', e.target.value.replace(/[^\d.,]/g, ''))}
                placeholder="0.00"
                className={cx(inputClass, 'font-display text-[28px] font-semibold')}
              />
            </Field>
            <label className="w-28 shrink-0">
              <span className="sr-only">Currency</span>
              <input
                list="currencies"
                value={form.currency}
                onChange={(e) => set('currency', e.target.value.toUpperCase().slice(0, 3))}
                className={cx(inputClass, 'text-center font-semibold')}
              />
              <datalist id="currencies">
                {CURRENCIES.map((c) => (
                  <option key={c} value={c} />
                ))}
              </datalist>
            </label>
          </div>

          <Field label="Merchant">
            <input value={form.merchant} onChange={(e) => set('merchant', e.target.value)} placeholder="Where?" className={inputClass} />
          </Field>

          <Field label="Date">
            <input type="date" value={form.date} onChange={(e) => set('date', e.target.value)} className={inputClass} />
          </Field>

          <div className="space-y-1.5">
            <span className="block px-1 text-[13px] text-ink-300">Category</span>
            <div className="flex flex-wrap gap-2">
              {EXPENSE_CATEGORIES.map((c) => (
                <Chip key={c} active={form.category === c} onClick={() => set('category', c)}>
                  <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[c] }} />
                  {c}
                </Chip>
              ))}
            </div>
          </div>

          {expense?.source === 'email' && (
            <p className="flex items-center gap-2 rounded-row bg-signal/10 px-3 py-2 text-[13px] text-signal">
              <Icon name="mail" size={15} className="shrink-0" />
              <span className="min-w-0 truncate">Imported from {emailMailbox(expense.source_ref) ?? 'email'}</span>
            </p>
          )}

          <Field label="Note">
            <input value={form.note} onChange={(e) => set('note', e.target.value)} placeholder="Optional" className={inputClass} />
          </Field>

          <div className="space-y-2">
            <button type="button" onClick={() => setShowItems((v) => !v)} className="press flex min-h-[44px] items-center gap-1 px-1 text-[14px] text-ink-300">
              <Icon name={showItems ? 'up' : 'down'} size={16} />
              Items {form.items.length > 0 && `(${form.items.length})`}
            </button>
            {showItems && (
              <div className="screen-in-up space-y-2">
                {form.items.map((it, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input
                      aria-label="Item name"
                      value={it.name}
                      onChange={(e) => set('items', form.items.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                      className={cx(inputClass, 'min-w-0 flex-1')}
                    />
                    <input
                      aria-label="Quantity"
                      inputMode="decimal"
                      value={it.qty ?? ''}
                      onChange={(e) => set('items', form.items.map((x, j) => (j === i ? { ...x, qty: e.target.value ? Number(e.target.value) : null } : x)))}
                      placeholder="qty"
                      className={cx(inputClass, 'w-16 px-2 text-center')}
                    />
                    <input
                      aria-label="Price"
                      inputMode="decimal"
                      value={it.price ?? ''}
                      onChange={(e) => set('items', form.items.map((x, j) => (j === i ? { ...x, price: e.target.value ? Number(e.target.value) : null } : x)))}
                      placeholder="price"
                      className={cx(inputClass, 'w-20 px-2 text-right')}
                    />
                    <IconButton icon="x" label="Remove item" onClick={() => set('items', form.items.filter((_, j) => j !== i))} />
                  </div>
                ))}
                <Button variant="ghost" icon="plus" onClick={() => set('items', [...form.items, { name: '', qty: null, price: null }])}>
                  Add item
                </Button>
              </div>
            )}
          </div>
        </div>
      )}
    </Sheet>
  );
}
