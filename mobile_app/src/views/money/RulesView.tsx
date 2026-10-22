import { useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, EmptyState, ErrorNote, Field, IconButton, Segmented, SkeletonList, inputClass, riseStyle } from '../../components/ui';
import { announce, useTopic } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { normaliseMerchant, ruleActionLabel } from '../../lib/moneyTools';
import type { MerchantRule, RuleMatch } from '../../lib/moneyTypes';
import { usePullRefresh } from '../../lib/nav';
import { EXPENSE_CATEGORIES, type ExpenseCategory } from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';

function RuleSheet({ open, onClose, rule }: { open: boolean; onClose: () => void; rule: MerchantRule | null }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [pattern, setPattern] = useState(rule?.pattern ?? '');
  const [match, setMatch] = useState<RuleMatch>(rule?.match ?? 'contains');
  const [category, setCategory] = useState<ExpenseCategory | null>(rule?.category ?? null);
  const [rename, setRename] = useState(rule?.rename ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      announce('money', 'expenses');
      notify(done, 'ok');
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (normaliseMerchant(pattern).length < 2) return setError('Type the merchant text to match (2 letters at least).');
    if (!category && !rename.trim()) return setError('Pick a category, a display name, or both.');
    const body = { pattern, match, category, rename: rename.trim() || null };
    void run(() => (rule ? client.updateMerchantRule(rule.id, body) : client.saveMerchantRule(body)), rule ? 'Rule saved' : 'Rule added');
  };

  const applyPast = async () => {
    if (!rule) return;
    if (!(await confirm({ title: `Apply to ${rule.pending} past expense${rule.pending === 1 ? '' : 's'}?`, body: `They would ${ruleActionLabel(rule)}.`, confirmLabel: 'Apply', tone: 'primary' }))) return;
    void run(() => client.applyMerchantRule(rule.id), `${rule.pending} past expense${rule.pending === 1 ? '' : 's'} updated`);
  };

  const remove = async () => {
    if (!rule) return;
    if (!(await confirm({ title: `Delete the rule for “${rule.pattern}”?`, body: 'Expenses already filed keep their category.', confirmLabel: 'Delete rule', tone: 'danger' }))) return;
    void run(() => client.deleteMerchantRule(rule.id), 'Rule deleted');
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={rule ? 'Edit rule' : 'New rule'}
      headerAction={rule ? <IconButton icon="trash" label="Delete rule" onClick={() => void remove()} className="hover:text-danger-400" /> : undefined}
      footer={
        <Button variant="primary" className="w-full" onClick={save} disabled={busy}>
          {busy ? 'Saving…' : 'Save rule'}
        </Button>
      }
    >
      <div className="space-y-4">
        {error && <ErrorNote message={error} />}
        <Field label="When the merchant" hint={pattern ? `Matched as “${normaliseMerchant(pattern)}” (case, accents and punctuation ignored).` : undefined}>
          <input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="MARJANE, GLOVO, inwi…" className={inputClass} autoFocus={!rule} />
        </Field>
        <Segmented
          value={match}
          onChange={setMatch}
          options={[
            { value: 'contains', label: 'Contains it' },
            { value: 'equals', label: 'Is exactly it' },
          ]}
        />
        <div className="space-y-1.5">
          <span className="block px-1 text-[13px] text-ink-300">File under</span>
          <div className="flex flex-wrap gap-2">
            <Chip active={category === null} onClick={() => setCategory(null)}>
              keep
            </Chip>
            {EXPENSE_CATEGORIES.map((c) => (
              <Chip key={c} active={category === c} onClick={() => setCategory(c)}>
                <span className="h-2 w-2 rounded-full" style={{ background: CATEGORY_COLOR[c] }} />
                {c}
              </Chip>
            ))}
          </div>
        </div>
        <Field label="Show it as (optional)">
          <input value={rename} onChange={(e) => setRename(e.target.value)} placeholder="Marjane" className={inputClass} />
        </Field>
        {rule && rule.pending > 0 && (
          <Button icon="refresh" className="w-full" onClick={() => void applyPast()} disabled={busy}>
            Apply to {rule.pending} past expense{rule.pending === 1 ? '' : 's'}
          </Button>
        )}
      </div>
    </Sheet>
  );
}

/** Money › Rules: "always file <merchant> under <category>", applied to every new expense. */
export default function RulesView() {
  const [sheet, setSheet] = useState<{ id: number; open: boolean; rule: MerchantRule | null } | null>(null);
  const next = useRef(1);
  const data = useHubData((c, s) => c.merchantRules(s), []);
  useTopic(['money', 'expenses'], data.reload);
  usePullRefresh(data.reload);
  const open = (rule: MerchantRule | null) => setSheet({ id: next.current++, open: true, rule });
  const close = () => setSheet((s) => (s ? { ...s, open: false } : s));
  const rules = data.data ?? [];

  return (
    <div className="space-y-4">
      <Button variant="primary" icon="plus" className="w-full" onClick={() => open(null)}>
        New rule
      </Button>
      <p className="px-1 text-[13px] text-ink-400">
        Rules run on every new expense: receipts, email, bank SMS, voice and split shares. Changing an expense’s category also offers to make a rule.
      </p>
      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
      {data.loading && !data.data && <SkeletonList rows={3} />}
      {data.data && !rules.length && <EmptyState icon="tag" title="No rules yet" hint="Fix a category once (say MARJANE → groceries) and every next one is filed for you." />}
      {rules.length > 0 && (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {rules.map((r, i) => (
            <button key={r.id} type="button" onClick={() => open(r)} className="press rise flex min-h-[60px] w-full items-center gap-3 px-4 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full" style={{ background: `${r.category ? CATEGORY_COLOR[r.category] : '#8A8FBF'}22`, color: r.category ? CATEGORY_COLOR[r.category] : '#8A8FBF' }}>
                <Icon name="tag" size={18} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">
                  {r.match === 'equals' ? '= ' : ''}
                  {r.pattern}
                </span>
                <span className="block truncate text-[12px] text-ink-500">{ruleActionLabel(r)}</span>
              </span>
              {r.pending > 0 && <Badge tone="warn">{r.pending} to fix</Badge>}
            </button>
          ))}
        </div>
      )}
      {sheet && <RuleSheet key={sheet.id} open={sheet.open} rule={sheet.rule} onClose={close} />}
    </div>
  );
}
