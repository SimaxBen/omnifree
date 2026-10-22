// Pure helpers for the Money page's forecast, detected subscriptions and savings goals. No React, no I/O.

import type {
  ForecastConfidence,
  GoalsListing,
  MoneyForecast,
  SavingsGoal,
  SubscriptionCadence,
  SubscriptionSuggestion,
  TrackedSubscription,
} from './types';

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

const CONFIDENCES: ForecastConfidence[] = ['low', 'medium', 'high'];
const CADENCES: SubscriptionCadence[] = ['weekly', 'monthly', 'yearly'];

/** `overview.forecast`, or null when the hub sent none (an older hub). */
export function normalizeForecast(v: unknown): MoneyForecast | null {
  if (!isRecord(v) || typeof v.projected_spent !== 'number') return null;
  return {
    projected_spent: num(v.projected_spent),
    projected_left: num(v.projected_left),
    daily_avg: num(v.daily_avg),
    remaining_recurring: num(v.remaining_recurring),
    spent_so_far: num(v.spent_so_far),
    variable_spent: num(v.variable_spent),
    remaining_days: num(v.remaining_days),
    month_end: str(v.month_end),
    history_months: num(v.history_months),
    history_daily_avg: numOrNull(v.history_daily_avg),
    confidence: CONFIDENCES.includes(v.confidence as ForecastConfidence) ? (v.confidence as ForecastConfidence) : 'low',
  };
}

/** 1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st, 22nd, 31st. */
export function ordinal(n: number): string {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th';
  return `${n}${suffix}`;
}

/** "~4,200 MAD left on the 31st" (or "~1,300 MAD short by the 31st"). */
export function forecastHeadline(f: MoneyForecast): string {
  const day = Number(f.month_end.slice(8, 10)) || 31;
  const amount = Math.abs(Math.round(f.projected_left)).toLocaleString('en-GB');
  return f.projected_left >= 0 ? `~${amount} MAD left on the ${ordinal(day)}` : `~${amount} MAD short by the ${ordinal(day)}`;
}

export function confidenceLabel(c: ForecastConfidence): string {
  return { low: 'Rough guess', medium: 'Fair estimate', high: 'Solid estimate' }[c];
}

export function cadenceLabel(c: SubscriptionCadence): string {
  return { weekly: 'Every week', monthly: 'Every month', yearly: 'Every year' }[c];
}

/** `GET /money/subscriptions` rows: weekly / yearly ones being tracked (monthly ones live in Recurring) and the
 *  merchants marked "not a subscription" (restorable), each sorted by name. */
export function splitTracked(rows: TrackedSubscription[]): { tracked: TrackedSubscription[]; dismissed: TrackedSubscription[] } {
  const byName = (a: TrackedSubscription, b: TrackedSubscription) => a.name.localeCompare(b.name);
  return {
    tracked: rows.filter((t) => t.status === 'accepted' && t.active).sort(byName),
    dismissed: rows.filter((t) => t.status === 'dismissed').sort(byName),
  };
}

/** `GET /money/subscriptions/suggestions` items, junk dropped. */
export function normalizeSuggestions(body: unknown): SubscriptionSuggestion[] {
  const raw = isRecord(body) && Array.isArray(body.suggestions) ? body.suggestions : [];
  return raw.filter(isRecord).filter((s) => typeof s.key === 'string' && CADENCES.includes(s.cadence as SubscriptionCadence)).map((s) => ({
    key: str(s.key),
    name: str(s.name, str(s.key)),
    cadence: s.cadence as SubscriptionCadence,
    avg_amount: num(s.avg_amount),
    currency: str(s.currency, 'MAD'),
    avg_amount_mad: num(s.avg_amount_mad),
    monthly_mad: num(s.monthly_mad),
    occurrences: num(s.occurrences),
    first_date: str(s.first_date),
    last_date: str(s.last_date),
    next_date: str(s.next_date),
    day: numOrNull(s.day),
    category: str(s.category, 'other') as SubscriptionSuggestion['category'],
    confidence: num(s.confidence),
    level: CONFIDENCES.includes(s.level as ForecastConfidence) ? (s.level as ForecastConfidence) : 'low',
    sources: Array.isArray(s.sources) ? s.sources.filter((x): x is string => typeof x === 'string') : [],
    expense_ids: Array.isArray(s.expense_ids) ? s.expense_ids.filter((x): x is string => typeof x === 'string') : [],
  }));
}

export function normalizeGoal(g: Record<string, unknown>): SavingsGoal {
  const account = isRecord(g.account) && typeof g.account.id === 'string' ? { id: g.account.id, name: str(g.account.name) } : null;
  return {
    id: str(g.id),
    name: str(g.name),
    target: num(g.target),
    currency: str(g.currency, 'MAD'),
    saved: num(g.saved),
    remaining: num(g.remaining),
    progress: Math.min(1, Math.max(0, num(g.progress))),
    done: !!g.done,
    deadline: strOrNull(g.deadline),
    months_left: numOrNull(g.months_left),
    overdue: !!g.overdue,
    per_month_needed: numOrNull(g.per_month_needed),
    saved_mad: numOrNull(g.saved_mad),
    target_mad: numOrNull(g.target_mad),
    account_id: strOrNull(g.account_id),
    account,
    emoji: strOrNull(g.emoji),
    color: strOrNull(g.color),
    note: strOrNull(g.note),
    created_at: str(g.created_at),
    updated_at: str(g.updated_at),
  };
}

/** `GET /money/goals`. */
export function normalizeGoals(body: unknown): GoalsListing {
  const b = isRecord(body) ? body : {};
  const goals = Array.isArray(b.goals) ? b.goals.filter(isRecord).filter((g) => typeof g.id === 'string').map(normalizeGoal) : [];
  return { goals, total_saved_mad: num(b.total_saved_mad), total_target_mad: num(b.total_target_mad) };
}

function amount(n: number, currency: string): string {
  return `${Math.round(n).toLocaleString('en-GB')} ${currency}`;
}

/** What the goal asks for: "1,800 MAD / month for 5 months", "Reached", "Overdue: 3,000 MAD to go", "No deadline". */
export function goalPaceLabel(g: SavingsGoal): string {
  if (g.done) return 'Reached';
  if (g.overdue) return `Deadline passed: ${amount(g.remaining, g.currency)} to go`;
  if (g.per_month_needed === null) return `${amount(g.remaining, g.currency)} to go, no deadline`;
  const months = g.months_left ?? 1;
  return `${amount(g.per_month_needed, g.currency)} / month for ${months} month${months === 1 ? '' : 's'}`;
}

/** Whole percent for the progress bar label (never 100 % before the target is reached). */
export function goalPercent(g: SavingsGoal): number {
  const pct = Math.floor(g.progress * 100);
  return g.done ? 100 : Math.min(99, pct);
}

/** Emoji presets offered by the goal sheet. */
export const GOAL_EMOJIS = ['🎯', '✈️', '🏠', '🚗', '💻', '🎓', '💍', '🛟', '🕋', '🎁'];

/** Colour presets (on the night ledger palette). */
export const GOAL_COLORS = ['#F5B43C', '#5EE0A8', '#7FA8FF', '#FF8FA3', '#C79BFF', '#FFD166'];
