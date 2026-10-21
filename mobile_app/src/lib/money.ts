// Pure helpers for the Money page (incomes, recurring, accounts, budgets). No React, no I/O.

import type { Expense, FxSource, Income, MoneyOverview } from './types';

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

function arr<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

/** `GET /money/overview`, with safe defaults for anything missing. */
export function normalizeMoneyOverview(body: unknown, month: string): MoneyOverview {
  const b = isRecord(body) ? body : {};
  const income = isRecord(b.income) ? b.income : {};
  const spent = isRecord(b.spent) ? b.spent : {};
  const accounts = isRecord(b.accounts) ? b.accounts : {};
  const fx = isRecord(b.fx) ? b.fx : {};
  const source = ['live', 'cache', 'stale', 'fallback'].includes(String(fx.source)) ? (fx.source as FxSource) : 'fallback';
  return {
    month: typeof b.month === 'string' ? b.month : month,
    currency: typeof b.currency === 'string' ? b.currency : 'MAD',
    income: { total: num(income.total), items: arr(income.items) },
    spent: { total: num(spent.total), expenses: num(spent.expenses), recurring: num(spent.recurring), count: num(spent.count) },
    net: num(b.net),
    savings_rate: typeof b.savings_rate === 'number' ? b.savings_rate : null,
    categories: arr(b.categories),
    series: arr<Record<string, unknown>>(b.series)
      .filter(isRecord)
      .map((s) => ({ month: String(s.month ?? ''), income: num(s.income), spent: num(s.spent) })),
    recurring: arr(b.recurring),
    upcoming: arr(b.upcoming),
    accounts: { total: num(accounts.total), items: arr(accounts.items) },
    fx: {
      base: typeof fx.base === 'string' ? fx.base : 'MAD',
      source,
      fetched_at: typeof fx.fetched_at === 'string' ? fx.fetched_at : null,
      unconverted: arr<unknown>(fx.unconverted).filter((x): x is string => typeof x === 'string'),
    },
  };
}

/** "62%", or "--" when there is no income to compare with. */
export function savingsLabel(rate: number | null): string {
  return rate === null ? '--' : `${Math.round(rate * 100)}%`;
}

/** Bar heights (px) for the cash-flow chart, scaled on the largest value of the series. */
export function cashflowBars(series: { income: number; spent: number }[], height: number): { income: number; spent: number }[] {
  const max = Math.max(1, ...series.flatMap((s) => [s.income, s.spent]));
  const h = (v: number) => (v > 0 ? Math.max(2, Math.round((v / max) * height)) : 0);
  return series.map((s) => ({ income: h(s.income), spent: h(s.spent) }));
}

/** Short amount for chart labels: 950, 12.4k, 1.2M. */
export function compactAmount(n: number): string {
  const a = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (a >= 1_000_000) return `${sign}${(a / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (a >= 10_000) return `${sign}${Math.round(a / 1000)}k`;
  if (a >= 1000) return `${sign}${(a / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${sign}${Math.round(a)}`;
}

/** "1 200,50" / "1,200.50" / "45" -> number; null when it is not an amount. */
export function parseAmount(text: string): number | null {
  let t = text.replace(/[\s ]/g, '').replace(/[^\d,.-]/g, '');
  if (!t || !/\d/.test(t)) return null;
  if (t.includes(',') && t.includes('.')) {
    // The last separator is the decimal one.
    t = t.lastIndexOf(',') > t.lastIndexOf('.') ? t.replace(/\./g, '').replace(',', '.') : t.replace(/,/g, '');
  } else if (t.includes(',')) {
    const parts = t.split(',');
    t = parts.length === 2 && parts[1].length !== 3 ? parts.join('.') : parts.join('');
  }
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** What a voice capture recorded, in words: "2 expenses and 1 income". */
export function voiceMoneySummary(expenses: Expense[] = [], incomes: Income[] = []): string {
  const parts: string[] = [];
  if (expenses.length) parts.push(`${expenses.length} expense${expenses.length === 1 ? '' : 's'}`);
  if (incomes.length) parts.push(`${incomes.length} income${incomes.length === 1 ? '' : 's'}`);
  return parts.length ? parts.join(' and ') : 'No amount heard';
}

function shortMonth(month: string): string {
  const [y, m] = month.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' });
}

/** "Since Oct 2026", "Oct 2026 to Mar 2027". */
export function monthRangeLabel(start: string, end: string | null): string {
  return end ? `${shortMonth(start)} to ${shortMonth(end)}` : `Since ${shortMonth(start)}`;
}

/** One-letter month label for chart axes. */
export function monthInitial(month: string): string {
  const [y, m] = month.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' }).slice(0, 3);
}

export function fxLabel(source: FxSource): string {
  return { live: 'Live exchange rates', cache: 'Exchange rates from today', stale: 'Exchange rates older than a day', fallback: 'Approximate exchange rates (offline)' }[source];
}

// ------------------------------------------------------------------ bank SMS

/** JSON body for SMS-forwarder apps: the placeholders are filled in by the app. */
export const SMS_FORWARDER_TEMPLATE = '{"from":"%from%","text":"%text%","receivedStamp":"%receivedStamp%"}';

/** Webhook URL to paste in the forwarder app; with `token` the secret travels as `?token=`. */
export function smsWebhookUrl(base: string, path: string, token?: string | null): string {
  const url = `${base.replace(/\/+$/, '')}${path.startsWith('/') ? path : `/${path}`}`;
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

/** "abcd••••••••" so the token can be shown without being readable. */
export function maskSecret(secret: string): string {
  return secret.length <= 4 ? '••••' : `${secret.slice(0, 4)}${'•'.repeat(Math.min(12, secret.length - 4))}`;
}

export type SmsTone = 'ok' | 'warn' | 'bad' | 'neutral';

/** What became of one SMS, in a few words, and how to colour it. */
export function smsOutcome(entry: { status: string; kind: string | null; reason?: string | null; ignored?: string }): { label: string; tone: SmsTone } {
  if (entry.status === 'error') return { label: entry.reason || 'Error', tone: 'bad' };
  if (entry.status === 'ignored') {
    const reason = entry.ignored || entry.reason || 'Ignored';
    return { label: reason.charAt(0).toUpperCase() + reason.slice(1), tone: /same|already/i.test(reason) ? 'neutral' : 'warn' };
  }
  const label = { expense: 'Expense', income: 'Income', withdrawal: 'Moved to cash' }[entry.kind ?? ''] ?? 'Recorded';
  return { label, tone: 'ok' };
}
