// Pure helpers for the Summary view, the Home "This week" card and the Gmail import. No React, no I/O.

import { currenciesByWeight, formatMoney } from './apps';
import type { Digest, InboxSyncResult } from './types';

/** "14 Oct – 20 Oct" (both ends are YYYY-MM-DD local days). */
export function rangeLabel(from: string, to: string): string {
  const fmt = (iso: string) =>
    new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  if (!from || !to) return '';
  return from === to ? fmt(from) : `${fmt(from)} – ${fmt(to)}`;
}

/** "just now", "12 min ago", "3 h ago", or a date for older stamps. */
export function ago(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((now.getTime() - t) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}

export interface HeadlineStat {
  label: string;
  value: string;
}

/** The two or three numbers the Home card shows: spending, tasks done, notes or uploads. */
export function headlineStats(d: Digest): HeadlineStat[] {
  const totals = d.stats.expenses.total_by_currency;
  const main = currenciesByWeight(totals)[0];
  const out: HeadlineStat[] = [
    { label: 'Spent', value: main ? formatMoney(Math.round(totals[main]), main) : 'Nothing' }, // whole units: the headline is narrow
    { label: 'Tasks done', value: String(d.stats.tasks.done) },
  ];
  if (d.stats.notes.created || !d.stats.drive.uploaded_files) out.push({ label: 'Notes', value: String(d.stats.notes.created) });
  else out.push({ label: 'Uploads', value: String(d.stats.drive.uploaded_files) });
  return out;
}

/** Shares (0..1) of done / open / overdue for the tasks bar; open excludes the overdue ones. */
export function taskShares(t: Digest['stats']['tasks']): { done: number; open: number; overdue: number } {
  const open = Math.max(t.open - t.overdue, 0);
  const total = t.done + open + t.overdue;
  if (!total) return { done: 0, open: 0, overdue: 0 };
  return { done: t.done / total, open: open / total, overdue: t.overdue / total };
}

/** LLM providers by request count, biggest first, with their share of the biggest. */
export function providerBars(byProvider: Record<string, number>): { provider: string; count: number; share: number }[] {
  const rows = Object.entries(byProvider)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1]);
  const max = rows[0]?.[1] ?? 0;
  return rows.map(([provider, count]) => ({ provider, count, share: max ? count / max : 0 }));
}

/** Skip reasons with their counts, the duplicates last (they are the least interesting). */
export function skipReasons(r: Pick<InboxSyncResult, 'skipped'>): { reason: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const s of r.skipped) counts.set(s.reason, (counts.get(s.reason) ?? 0) + 1);
  const dup = (reason: string) => reason.startsWith('already');
  return [...counts.entries()]
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => Number(dup(a.reason)) - Number(dup(b.reason)) || b.count - a.count);
}

/** One line for a toast: "2 new expenses, 5 skipped" (+ errors / left for next run). */
export function syncSummary(r: InboxSyncResult): string {
  const n = r.created.length;
  const parts = [n ? `${n} new expense${n === 1 ? '' : 's'}${r.dry_run ? ' found' : ''}` : 'No new receipts'];
  if (r.skipped.length) parts.push(`${r.skipped.length} skipped`);
  if (r.errors.length) parts.push(`${r.errors.length} failed`);
  if (r.remaining) parts.push(`${r.remaining} left for next run`);
  return parts.join(', ');
}

/** Schedule template for the daily Gmail import (target on this hub, the bearer added by the form). */
export function gmailSyncTemplate(hubUrl: string): { name: string; preset: string; target: string; method: string; body: string } {
  return {
    name: 'gmail-receipts',
    preset: 'daily',
    target: `${hubUrl.replace(/\/+$/, '')}/inbox/receipts/sync`,
    method: 'POST',
    body: '{"days": 2, "max": 15}',
  };
}

/** Headers of a scheduled call: JSON content type with a body, and the hub bearer when asked for. */
export function scheduleHeaders(opts: { body?: string | null; token?: string; withToken?: boolean }): Record<string, string> | undefined {
  const headers: Record<string, string> = {};
  if (opts.body) headers['Content-Type'] = 'application/json';
  if (opts.withToken && opts.token) headers.Authorization = `Bearer ${opts.token}`;
  return Object.keys(headers).length ? headers : undefined;
}

/** The mailbox an imported expense came from (`source_ref` is `<mailbox>#<Message-ID>`). */
export function emailMailbox(sourceRef: string | null | undefined): string | null {
  if (!sourceRef) return null;
  const i = sourceRef.indexOf('#');
  if (i <= 0) return null;
  const box = sourceRef.slice(0, i);
  return box.includes('@') ? box : null;
}
