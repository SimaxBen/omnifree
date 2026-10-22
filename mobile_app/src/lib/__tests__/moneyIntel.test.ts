import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient } from '../api';
import { normalizeMoneyOverview } from '../money';
import {
  cadenceLabel,
  confidenceLabel,
  forecastHeadline,
  goalPaceLabel,
  goalPercent,
  normalizeForecast,
  normalizeGoal,
  normalizeGoals,
  normalizeSuggestions,
  ordinal,
  splitTracked,
} from '../moneyIntel';
import type { MoneyForecast, TrackedSubscription } from '../types';

const forecast: MoneyForecast = {
  projected_spent: 4345,
  projected_left: 5655.4,
  daily_avg: 30,
  remaining_recurring: 350,
  spent_so_far: 3725,
  variable_spent: 660,
  remaining_days: 9,
  month_end: '2026-10-31',
  history_months: 1,
  history_daily_avg: 30,
  confidence: 'high',
};

describe('forecast helpers', () => {
  it('writes ordinals', () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 28, 30, 31].map(ordinal)).toEqual([
      '1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '28th', '30th', '31st',
    ]);
  });

  it('phrases the headline, short or not', () => {
    expect(forecastHeadline(forecast)).toBe('~5,655 MAD left on the 31st');
    expect(forecastHeadline({ ...forecast, projected_left: -1299.6, month_end: '2027-02-28' })).toBe('~1,300 MAD short by the 28th');
    expect(confidenceLabel('low')).toBe('Rough guess');
  });

  it('normalises the overview forecast, null when missing', () => {
    expect(normalizeForecast(forecast)).toEqual(forecast);
    expect(normalizeForecast({ projected_spent: 10, confidence: 'weird' })).toMatchObject({ projected_left: 0, confidence: 'low', history_daily_avg: null });
    expect(normalizeForecast(null)).toBeNull();
    expect(normalizeForecast({ projected_left: 1 })).toBeNull();
    expect(normalizeMoneyOverview({ forecast }, '2026-10').forecast).toEqual(forecast);
    expect(normalizeMoneyOverview({}, '2026-10').forecast).toBeNull();
  });
});

describe('subscriptions and goals', () => {
  it('keeps valid suggestions only', () => {
    const list = normalizeSuggestions({
      suggestions: [
        { key: 'netflix', name: 'Netflix', cadence: 'monthly', avg_amount: 65, currency: 'MAD', next_date: '2026-11-14', day: 14, confidence: 0.95, level: 'high', sources: ['sms', 3] },
        { key: 'x', cadence: 'daily' },
        'junk',
      ],
    });
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({ key: 'netflix', day: 14, level: 'high', sources: ['sms'], expense_ids: [] });
    expect(normalizeSuggestions(null)).toEqual([]);
    expect(cadenceLabel('yearly')).toBe('Every year');
  });

  it('describes a goal’s pace', () => {
    const base = normalizeGoal({ id: 'g', name: 'Japan', target: 12000, saved: 3000, remaining: 9000, progress: 0.25, currency: 'MAD', per_month_needed: 1800, months_left: 5 });
    expect(goalPaceLabel(base)).toBe('1,800 MAD / month for 5 months');
    expect(goalPaceLabel({ ...base, months_left: 1, per_month_needed: 9000 })).toBe('9,000 MAD / month for 1 month');
    expect(goalPaceLabel({ ...base, per_month_needed: null })).toBe('9,000 MAD to go, no deadline');
    expect(goalPaceLabel({ ...base, overdue: true })).toBe('Deadline passed: 9,000 MAD to go');
    expect(goalPaceLabel({ ...base, done: true })).toBe('Reached');
    expect(goalPercent({ ...base, progress: 0.999 })).toBe(99);
    expect(goalPercent({ ...base, progress: 1, done: true })).toBe(100);
    expect(normalizeGoal({ id: 'g', progress: 7, account: { id: 'a1', name: 'CIH' } })).toMatchObject({ progress: 1, account: { id: 'a1', name: 'CIH' }, deadline: null });
    expect(normalizeGoals({ goals: [{ id: 'a' }, { name: 'no id' }], total_saved_mad: 5 })).toMatchObject({ total_saved_mad: 5, total_target_mad: 0 });
    expect(normalizeGoals({ goals: [{ id: 'a' }] }).goals).toHaveLength(1);
  });
});

describe('client: subscriptions and goals', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('calls the endpoints', async () => {
    const calls: { method: string; path: string; json?: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        const path = new URL(url).pathname.replace('/api', '');
        calls.push({ method: init.method ?? 'GET', path, json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined });
        if (init.method === 'DELETE') return new Response(null, { status: 204 });
        const body =
          path === '/money/goals' && init.method !== 'POST'
            ? { goals: [{ id: 'g1', name: 'Japan' }], total_saved_mad: 1 }
            : path.endsWith('/contribute')
              ? { goal: { id: 'g1', saved: 500 }, contribution: {} }
              : path === '/money/goals/g1'
                ? { id: 'g1', contributions: [{ id: 'c1', amount: 5 }] }
                : path === '/money/subscriptions'
                  ? { subscriptions: [{ id: 's1' }] }
                  : { suggestions: [], id: 'g1' };
        return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
      }),
    );
    const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });
    expect(await client.subscriptionSuggestions()).toEqual([]);
    expect(await client.trackedSubscriptions()).toEqual([{ id: 's1' }]);
    await client.acceptSubscription({ key: 'netflix', name: 'Netflix' });
    await client.dismissSubscription('claude');
    await client.forgetSubscription('s 1');
    expect((await client.goals()).goals[0].name).toBe('Japan');
    expect((await client.goal('g1')).contributions).toHaveLength(1);
    await client.createGoal({ name: 'Japan', target: 12000 });
    await client.updateGoal('g1', { deadline: null });
    expect((await client.contributeGoal('g1', -50)).saved).toBe(500);
    await client.deleteGoal('g1');
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      'GET /money/subscriptions/suggestions',
      'GET /money/subscriptions',
      'POST /money/subscriptions/accept',
      'POST /money/subscriptions/dismiss',
      'DELETE /money/subscriptions/s%201',
      'GET /money/goals',
      'GET /money/goals/g1',
      'POST /money/goals',
      'PATCH /money/goals/g1',
      'POST /money/goals/g1/contribute',
      'DELETE /money/goals/g1',
    ]);
    expect(calls[3].json).toEqual({ key: 'claude' });
    expect(calls[9].json).toEqual({ amount: -50, note: null });
  });
});

describe('splitTracked', () => {
  const row = (id: string, name: string, status: TrackedSubscription['status'], active: boolean): TrackedSubscription => ({
    id, key: id, name, status, cadence: 'weekly', amount: 50, currency: 'MAD', category: 'bills', anchor_date: null,
    next_date: null, active, recurring_id: null, created_at: 'x', updated_at: 'x',
  });

  it('keeps tracked weekly / yearly ones and the restorable dismissals apart', () => {
    const out = splitTracked([
      row('a', 'Tram', 'accepted', true),
      row('b', 'Netflix', 'accepted', false), // monthly: shown in Recurring
      row('c', 'Spotify', 'dismissed', false),
      row('d', 'Basic Fit', 'accepted', true),
      row('e', 'Amazon', 'dismissed', false),
    ]);
    expect(out.tracked.map((t) => t.name)).toEqual(['Basic Fit', 'Tram']);
    expect(out.dismissed.map((t) => t.name)).toEqual(['Amazon', 'Spotify']);
    expect(splitTracked([])).toEqual({ tracked: [], dismissed: [] });
  });
});
