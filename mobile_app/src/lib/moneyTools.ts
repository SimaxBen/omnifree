// Pure helpers for the money tools (rules, debts, splits, receipt search, export). No React, no I/O.

import type { Debt, DebtListing, SplitMember, VoiceDebtOutcome } from './moneyTypes';
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
export function voiceDebtLine(d: Pick<VoiceDebtOutcome, 'type' | 'action' | 'person' | 'unapplied' | 'currency'>): string {
  if (d.action === 'unmatched') return `No open loan with ${d.person} (nothing recorded)`;
  if (d.action === 'duplicate') return `Already recorded a moment ago (${d.person}), not added twice`;
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

export type VoiceDebtUndo = { kind: 'debt'; id: string } | { kind: 'payment'; debtId: string; paymentId: string };

/** What Undo removes for the debts of a voice memo: the loans it created and the repayments it recorded. */
export function voiceDebtUndo(outcomes: VoiceDebtOutcome[]): VoiceDebtUndo[] {
  const ops: VoiceDebtUndo[] = [];
  for (const o of outcomes) {
    if (o.action === 'created') {
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

/** Equal split in cents, leftover cents to the first members (same as the hub). */
export function equalShares(amount: number, ids: string[]): Record<string, number> {
  if (!ids.length) return {};
  const total = Math.round(amount * 100);
  const base = Math.floor(total / ids.length);
  const extra = total - base * ids.length;
  return Object.fromEntries(ids.map((id, i) => [id, (base + (i < extra ? 1 : 0)) / 100]));
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

/** Member id -> display name ("You" for the owner). */
export function memberNames(members: SplitMember[]): Record<string, string> {
  return Object.fromEntries(members.map((m) => [m.id, m.me ? 'You' : m.name]));
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
