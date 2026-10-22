// Pure helpers for the money tools (rules, debts, splits, receipt search, export). No React, no I/O.

import type { Debt, DebtListing, SplitMember, SplitMode, SplitPair, VoiceDebtOutcome } from './moneyTypes';
import type { ExpenseCategory } from './types';

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

// ------------------------------------------------------------------ merchant rules

/** Same normalisation as the hub: lower case, no accents, punctuation as spaces. */
export function normaliseMerchant(value: string | null | undefined): string {
  return (value ?? '')
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^0-9a-z؀-ۿ]+/g, ' ')
    .trim();
}

export interface RuleSuggestion {
  /** The merchant as it arrives from the source (bank SMS, email...): what the rule matches. */
  pattern: string;
  category: ExpenseCategory | null;
  rename: string | null;
}

/**
 * After an edit: the rule to offer ("Always do this for <merchant>?"), or null when nothing worth a rule changed
 * (no merchant before, or neither the category nor the name changed).
 */
export function ruleSuggestion(
  before: { merchant: string; category: ExpenseCategory },
  after: { merchant: string; category: ExpenseCategory },
): RuleSuggestion | null {
  const original = before.merchant.trim();
  if (normaliseMerchant(original).length < 2) return null;
  const category = after.category !== before.category ? after.category : null;
  const renamed = after.merchant.trim();
  const rename = renamed && normaliseMerchant(renamed) !== normaliseMerchant(original) ? renamed : null;
  if (!category && !rename) return null;
  return { pattern: original, category, rename };
}

/** "Category → groceries, shown as Marjane". */
export function ruleActionLabel(rule: { category: string | null; rename: string | null }): string {
  const parts: string[] = [];
  if (rule.category) parts.push(`file under ${rule.category}`);
  if (rule.rename) parts.push(`show as “${rule.rename}”`);
  return parts.join(', ') || 'no action';
}

// ------------------------------------------------------------------ debts

/** `GET /money/debts` with safe defaults. */
export function normalizeDebts(body: unknown): DebtListing {
  const b = isRecord(body) ? body : {};
  const s = isRecord(b.summary) ? b.summary : {};
  return {
    debts: Array.isArray(b.debts) ? (b.debts as Debt[]) : [],
    summary: {
      currency: typeof s.currency === 'string' ? s.currency : 'MAD',
      owed_to_me: num(s.owed_to_me),
      i_owe: num(s.i_owe),
      net: num(s.net),
      by_person: Array.isArray(s.by_person) ? (s.by_person as DebtListing['summary']['by_person']) : [],
      unconverted: Array.isArray(s.unconverted) ? s.unconverted.filter((x): x is string => typeof x === 'string') : [],
    },
  };
}

/** "Ayoub owes you" / "You owe Karim". */
export function debtHeadline(d: Pick<Debt, 'person' | 'direction'>): string {
  return d.direction === 'lent' ? `${d.person} owes you` : `You owe ${d.person}`;
}

/** Days between two ISO days (b - a). */
function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T12:00:00Z`) - Date.parse(`${a}T12:00:00Z`)) / 86_400_000);
}

/** "Lent 12 days ago · due in 3 days" style detail. */
export function debtDetail(d: Pick<Debt, 'direction' | 'date' | 'due_date' | 'settled' | 'paid' | 'currency'>, today: string): string {
  const age = daysBetween(d.date, today);
  const verb = d.direction === 'lent' ? 'Lent' : 'Borrowed';
  const parts = [age <= 0 ? `${verb} today` : age === 1 ? `${verb} yesterday` : `${verb} ${age} days ago`];
  if (d.paid > 0 && !d.settled) parts.push(`${Math.round(d.paid * 100) / 100} ${d.currency} back`);
  if (d.due_date && !d.settled) {
    const left = daysBetween(today, d.due_date);
    parts.push(left < 0 ? `${-left} day${left === -1 ? '' : 's'} late` : left === 0 ? 'due today' : `due in ${left} day${left === 1 ? '' : 's'}`);
  }
  if (d.settled) parts.push('settled');
  return parts.join(' · ');
}

/** Open money lent more than `days` ago (what the Monday push lists). */
export function staleLent(debts: Debt[], today: string, days = 14): Debt[] {
  return debts.filter((d) => d.direction === 'lent' && !d.settled && d.remaining > 0 && daysBetween(d.date, today) > days);
}

/** The `debts` of a voice result (money or notes voice), [] when missing. */
export function voiceDebtsOf(body: unknown): VoiceDebtOutcome[] {
  const list = isRecord(body) && Array.isArray(body.debts) ? body.debts : [];
  return list.filter((d): d is VoiceDebtOutcome => isRecord(d) && typeof d.person === 'string' && typeof d.type === 'string');
}

/** "Lent to Ayoub", "Ayoub paid you back", "No open loan with Sara (nothing recorded)". */
export function voiceDebtLine(d: Pick<VoiceDebtOutcome, 'type' | 'action' | 'person' | 'unapplied' | 'currency' | 'split'>): string {
  if (d.action === 'unmatched') return `No open loan with ${d.person} (nothing recorded)`;
  if (d.action === 'duplicate') return `Already recorded a moment ago (${d.person}), not added twice`;
  if (d.type === 'split') {
    const where = d.split ? ` in ${d.split.group_name}${d.split.group_created ? ' (new group)' : ''}` : '';
    return `Shared with ${d.person}${where}`;
  }
  const extra = d.unapplied > 0 ? ` (${d.unapplied} ${d.currency} more than owed)` : '';
  switch (d.type) {
    case 'lend':
      return `Lent to ${d.person}`;
    case 'borrow':
      return `Borrowed from ${d.person}`;
    case 'repaid':
      return `${d.person} paid you back${extra}`;
    default:
      return `You paid ${d.person} back${extra}`;
  }
}

export type VoiceDebtUndo =
  | { kind: 'debt'; id: string }
  | { kind: 'payment'; debtId: string; paymentId: string }
  /** A shared expense heard by voice: delete it (and the group when the memo created it). */
  | { kind: 'split'; groupId: string; entryId: string; groupCreated: boolean };

/** What Undo removes for the debts of a voice memo: the loans it created and the repayments it recorded. */
export function voiceDebtUndo(outcomes: VoiceDebtOutcome[]): VoiceDebtUndo[] {
  const ops: VoiceDebtUndo[] = [];
  for (const o of outcomes) {
    if (o.type === 'split') {
      if (o.action === 'created' && o.split) ops.push({ kind: 'split', groupId: o.split.group_id, entryId: o.split.entry_id, groupCreated: o.split.group_created });
    } else if (o.action === 'created') {
      for (const d of o.debts) ops.push({ kind: 'debt', id: d.id });
    } else if (o.action === 'repaid') {
      (o.payment_ids ?? []).forEach((paymentId, i) => {
        const debt = o.debts[i];
        if (debt && paymentId) ops.push({ kind: 'payment', debtId: debt.id, paymentId });
      });
    }
  }
  return ops;
}

// ------------------------------------------------------------------ splits

const cents = (x: number) => Math.round(x * 100);
const fixed = (c: number) => (c / 100).toFixed(2);

/**
 * `total` cents split in proportion to `weights` (rounded down to the cent); the cents left over go to the payer when
 * they have a share, else to the last member with one. Same rule as the hub, so previews match what is stored.
 */
export function allocateCents(total: number, weights: [string, number][], payer?: string | null): Record<string, number> {
  const live = weights.filter(([, w]) => w > 0);
  if (!live.length) return {};
  const pot = Math.max(total, 0);
  const sum = live.reduce((a, [, w]) => a + w, 0);
  const out: Record<string, number> = Object.fromEntries(live.map(([id, w]) => [id, Math.floor((pot * w) / sum)]));
  const left = pot - Object.values(out).reduce((a, v) => a + v, 0);
  if (left) out[payer && payer in out ? payer : live[live.length - 1][0]] += left;
  return out;
}

/** Equal split to the cent; the leftover cents go to the payer (else the last member). */
export function equalShares(amount: number, ids: string[], payer?: string | null): Record<string, number> {
  const out = allocateCents(cents(amount), ids.map((id): [string, number] => [id, 1]), payer);
  return Object.fromEntries(Object.entries(out).map(([id, c]) => [id, c / 100]));
}

/** Percentages (to 0.01 %) to amounts; leftover cents as {@link equalShares}. `order` is the member order. */
export function percentShares(amount: number, percents: Record<string, number>, order: string[], payer?: string | null): Record<string, number> {
  const weights = order.filter((id) => id in percents).map((id): [string, number] => [id, Math.round((percents[id] ?? 0) * 100)]);
  const out = allocateCents(cents(amount), weights, payer);
  return Object.fromEntries(Object.entries(out).map(([id, c]) => [id, c / 100]));
}

export interface ShareDraft {
  amount: number;
  mode: SplitMode;
  /** Every member, in group order. */
  order: string[];
  /** Who is in this expense. */
  included: string[];
  payer: string;
  /** Custom amounts as typed (null: empty). */
  amounts: Record<string, number | null>;
  /** Custom percentages as typed (null: empty). */
  percents: Record<string, number | null>;
}

export interface ShareResult {
  /** The amount per included member (what would be stored; a preview while a custom split does not add up). */
  shares: Record<string, number>;
  /** Custom modes: what is left to assign (money for amounts, % for percentages); negative when too much. */
  remaining: number;
  unit: 'money' | 'percent' | null;
  error: string | null;
}

/** The live share calculator of the shared-expense sheet: shares, what is left and why it cannot be saved. */
export function computeShares(d: ShareDraft): ShareResult {
  const inc = d.order.filter((id) => d.included.includes(id));
  const total = cents(d.amount);
  const unit = d.mode === 'equal' ? null : d.mode === 'custom' ? 'money' : 'percent';
  const none: ShareResult = { shares: {}, remaining: 0, unit, error: null };
  if (!(d.amount > 0) || total <= 0) return { ...none, error: 'Enter the amount.' };
  if (!inc.length) return { ...none, error: 'Pick who shares it.' };
  if (d.mode === 'equal') return { ...none, shares: equalShares(d.amount, inc, d.payer) };
  if (d.mode === 'custom') {
    const vals = inc.map((id): [string, number] => [id, d.amounts[id] ?? 0]);
    if (vals.some(([, v]) => !Number.isFinite(v) || v < 0)) return { ...none, error: 'Shares cannot be negative.' };
    const shares = Object.fromEntries(vals.filter(([, v]) => cents(v) > 0).map(([id, v]) => [id, cents(v) / 100]));
    const left = total - vals.reduce((a, [, v]) => a + cents(v), 0);
    const error = !Object.keys(shares).length
      ? 'Give at least one person a share.'
      : left > 0
        ? `${fixed(left)} left to assign.`
        : left < 0
          ? `${fixed(-left)} too much.`
          : null;
    return { shares, remaining: left / 100, unit, error };
  }
  const vals = inc.map((id): [string, number] => [id, d.percents[id] ?? 0]);
  if (vals.some(([, v]) => !Number.isFinite(v) || v < 0 || v > 100)) return { ...none, error: 'Percentages go from 0 to 100.' };
  const bp = vals.map(([id, v]): [string, number] => [id, Math.round(v * 100)]);
  const left = 10000 - bp.reduce((a, [, v]) => a + v, 0);
  const error = !bp.some(([, v]) => v > 0)
    ? 'Give at least one person a share.'
    : left > 0
      ? `${fixed(left)} % left to assign.`
      : left < 0
        ? `${fixed(-left)} % too much.`
        : null;
  // While it does not add up the shares are only a preview: each one at its percentage of the amount.
  const shares = error
    ? Object.fromEntries(bp.filter(([, v]) => v > 0).map(([id, v]) => [id, Math.round((total * v) / 10000) / 100]))
    : Object.fromEntries(Object.entries(allocateCents(total, bp, d.payer)).map(([id, c]) => [id, c / 100]));
  return { shares, remaining: left / 100, unit, error };
}

/**
 * Auto-fill: what is left goes to the included members whose field is empty (shared like an equal split), else on
 * top of the payer's (or the last member's) value. Returns the new values of the mode's fields for the included
 * members, or null when there is nothing to fill (or already too much).
 */
export function autofillShares(d: ShareDraft): Record<string, number> | null {
  if (d.mode === 'equal') return null;
  const inc = d.order.filter((id) => d.included.includes(id));
  if (!inc.length || !(d.amount > 0)) return null;
  const field = d.mode === 'custom' ? d.amounts : d.percents;
  const unit = (v: number | null | undefined) => Math.round((v ?? 0) * 100); // cents, or basis points
  const target = d.mode === 'custom' ? cents(d.amount) : 10000;
  const left = target - inc.reduce((a, id) => a + unit(field[id]), 0);
  if (left <= 0) return null;
  const out: Record<string, number> = Object.fromEntries(inc.map((id) => [id, unit(field[id])]));
  const empty = inc.filter((id) => out[id] === 0);
  if (empty.length) {
    const add = allocateCents(left, empty.map((id): [string, number] => [id, 1]), d.payer);
    for (const [id, v] of Object.entries(add)) out[id] += v;
  } else {
    out[d.payer in out ? d.payer : inc[inc.length - 1]] += left;
  }
  return Object.fromEntries(Object.entries(out).map(([id, v]) => [id, v / 100]));
}

/** What an expense puts on the Debts page: "Ayoub will owe you 20.00", "You will owe Sara 45.00". */
export function shareDebtLines(shares: Record<string, number>, payer: string, me: string, names: Record<string, string>): string[] {
  if (payer === me) {
    return Object.entries(shares)
      .filter(([id, v]) => id !== me && cents(v) > 0)
      .map(([id, v]) => `${names[id] ?? '?'} will owe you ${fixed(cents(v))}`);
  }
  const mine = shares[me] ?? 0;
  return cents(mine) > 0 ? [`You will owe ${names[payer] ?? '?'} ${fixed(cents(mine))}`] : [];
}

/** One owner pair in words: "Ayoub owes you 15.00 MAD", "You owe Sara 45.00 MAD", "All settled with Karim". */
export function pairLabel(p: Pick<SplitPair, 'name' | 'net'>, currency: string): string {
  const c = cents(p.net);
  if (c === 0) return `All settled with ${p.name}`;
  return c > 0 ? `${p.name} owes you ${fixed(c)} ${currency}` : `You owe ${p.name} ${fixed(-c)} ${currency}`;
}

/** Why custom shares cannot be saved, or null when they add up to the amount. */
export function customSharesError(amount: number, shares: Record<string, number>): string | null {
  const values = Object.values(shares).filter((v) => Number.isFinite(v));
  if (values.some((v) => v < 0)) return 'Shares cannot be negative.';
  const sum = values.reduce((a, v) => a + Math.round(v * 100), 0);
  if (sum === 0) return 'Give at least one person a share.';
  const diff = Math.round(amount * 100) - sum;
  if (diff === 0) return null;
  return diff > 0 ? `${(diff / 100).toFixed(2)} left to assign.` : `${(-diff / 100).toFixed(2)} too much.`;
}

/** Member id -> display name ("You" for the owner, or `meLabel`). */
export function memberNames(members: SplitMember[], meLabel = 'You'): Record<string, string> {
  return Object.fromEntries(members.map((m) => [m.id, m.me ? meLabel : m.name]));
}

/** The owner's net in words: "You are owed 120.00", "You owe 45.00", "All settled". */
export function myBalanceLabel(net: number, currency: string): string {
  const cents = Math.round(net * 100);
  if (cents === 0) return 'All settled';
  return cents > 0 ? `You are owed ${(cents / 100).toFixed(2)} ${currency}` : `You owe ${(-cents / 100).toFixed(2)} ${currency}`;
}

// ------------------------------------------------------------------ receipt search

/** Splits `text` into plain and highlighted parts for every search term (case-insensitive). */
export function highlight(text: string, terms: string[]): { text: string; hit: boolean }[] {
  const clean = terms.map((t) => t.trim()).filter((t) => t.length >= 2);
  if (!text || !clean.length) return text ? [{ text, hit: false }] : [];
  const escaped = clean.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const re = new RegExp(`(${escaped.join('|')})`, 'gi');
  return text
    .split(re)
    .filter((part) => part !== '')
    .map((part) => ({ text: part, hit: clean.some((t) => t.toLowerCase() === part.toLowerCase()) }));
}
