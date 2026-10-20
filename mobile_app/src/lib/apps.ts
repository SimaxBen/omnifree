// Pure helpers for the personal apps (notes, tasks, expenses, budget). No React, no I/O.

import type { BudgetItem, BudgetStatus, Task } from './types';

/** The hub interprets "today" in this zone; the PWA does the same so due labels agree. */
export const HOME_TZ = 'Africa/Casablanca';

/** YYYY-MM-DD for `date` in `tz`. */
export function isoDay(date: Date = new Date(), tz = HOME_TZ): string {
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
  } catch {
    return date.toISOString().slice(0, 10);
  }
}

function dayNumber(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number);
  return Math.round(Date.UTC(y, m - 1, d) / 86_400_000);
}

export function addDays(iso: string, n: number): string {
  return new Date((dayNumber(iso) + n) * 86_400_000).toISOString().slice(0, 10);
}

export type DueTone = 'overdue' | 'today' | 'soon' | 'later' | 'none';

export function dueInfo(due: string | null, today: string): { label: string; tone: DueTone } {
  if (!due) return { label: 'No date', tone: 'none' };
  const diff = dayNumber(due) - dayNumber(today);
  if (diff < -1) return { label: `${-diff} days late`, tone: 'overdue' };
  if (diff === -1) return { label: 'Yesterday', tone: 'overdue' };
  if (diff === 0) return { label: 'Today', tone: 'today' };
  if (diff === 1) return { label: 'Tomorrow', tone: 'soon' };
  const d = new Date(dayNumber(due) * 86_400_000);
  if (diff < 7) return { label: d.toLocaleDateString('en-GB', { weekday: 'long', timeZone: 'UTC' }), tone: 'soon' };
  return { label: d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }), tone: 'later' };
}

const PRIORITY_RANK = { high: 0, normal: 1, low: 2 } as const;

/** Open tasks first; then due date ascending (no date last), priority, creation. */
export function sortTasks(tasks: Task[]): Task[] {
  return [...tasks].sort(
    (a, b) =>
      Number(a.done) - Number(b.done) ||
      (a.due ?? '9999').localeCompare(b.due ?? '9999') ||
      PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] ||
      a.created_at.localeCompare(b.created_at),
  );
}

// ------------------------------------------------------------------ months & money

export function currentMonth(today = isoDay()): string {
  return today.slice(0, 7);
}

export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split('-').map(Number);
  const idx = y * 12 + (m - 1) + delta;
  return `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, '0')}`;
}

export function monthLabel(month: string): string {
  const [y, m] = month.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' });
}

export function formatMoney(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat('en-GB', { style: 'currency', currency, currencyDisplay: 'code', maximumFractionDigits: 2 })
      .format(amount)
      .replace(/ /g, ' ');
  } catch {
    return `${amount.toFixed(2)} ${currency}`;
  }
}

export interface CategorySlice {
  category: string;
  amount: number;
  /** Share of the currency total, 0..1. */
  share: number;
}

/** Category totals for one currency, biggest first (the summary groups by category then currency). */
export function categoryBreakdown(byCategory: Record<string, Record<string, number>>, currency: string): CategorySlice[] {
  const rows = Object.entries(byCategory)
    .map(([category, amounts]) => ({ category, amount: amounts?.[currency] ?? 0 }))
    .filter((r) => r.amount > 0);
  const total = rows.reduce((s, r) => s + r.amount, 0);
  return rows.map((r) => ({ ...r, share: total ? r.amount / total : 0 })).sort((a, b) => b.amount - a.amount);
}

/** The currency with the largest total first, so the chart shows the one that matters. */
export function currenciesByWeight(totals: Record<string, number>): string[] {
  return Object.entries(totals)
    .sort((a, b) => b[1] - a[1])
    .map(([c]) => c);
}

// ------------------------------------------------------------------ budget

const STATUS_RANK: Record<BudgetStatus, number> = { over: 0, warn: 1, ok: 2, unknown: 3 };

export interface BudgetGroup {
  kind: string;
  items: BudgetItem[];
  worst: BudgetStatus;
}

export function groupBudget(items: BudgetItem[]): BudgetGroup[] {
  const map = new Map<string, BudgetItem[]>();
  for (const it of items) {
    const k = it.kind || 'other';
    map.set(k, [...(map.get(k) ?? []), it]);
  }
  return [...map.entries()]
    .map(([kind, list]) => {
      const sorted = [...list].sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status] || (b.fraction ?? -1) - (a.fraction ?? -1));
      return { kind, items: sorted, worst: sorted[0]?.status ?? 'unknown' };
    })
    .sort((a, b) => STATUS_RANK[a.worst] - STATUS_RANK[b.worst] || a.kind.localeCompare(b.kind));
}

/** Items at warn or over, the most used first: what Home shows. */
export function budgetAlerts(items: BudgetItem[]): BudgetItem[] {
  return items
    .filter((i) => i.status === 'warn' || i.status === 'over')
    .sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status] || (b.fraction ?? 0) - (a.fraction ?? 0));
}

export function greeting(date = new Date(), tz = HOME_TZ): string {
  let hour = date.getHours();
  try {
    hour = Number(new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: 'numeric', hourCycle: 'h23' }).format(date));
  } catch {
    // Keep the device hour.
  }
  if (hour < 5) return 'Still up';
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}
