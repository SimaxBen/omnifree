import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient } from '../api';
import {
  cashflowBars,
  compactAmount,
  fxLabel,
  maskSecret,
  monthInitial,
  monthRangeLabel,
  normalizeMoneyOverview,
  parseAmount,
  savingsLabel,
  smsOutcome,
  smsWebhookUrl,
  voiceMoneySummary,
} from '../money';
import type { Expense, Income } from '../types';

describe('money helpers', () => {
  it('normalises a partial overview with safe defaults', () => {
    const ov = normalizeMoneyOverview({ income: { total: 12000 }, series: [{ month: '2026-10', income: 12000, spent: 'x' }, 'junk'], fx: { source: 'weird', unconverted: ['XYZ', 3] } }, '2026-10');
    expect(ov.month).toBe('2026-10');
    expect(ov.income).toEqual({ total: 12000, items: [] });
    expect(ov.spent).toEqual({ total: 0, expenses: 0, recurring: 0, count: 0 });
    expect(ov.savings_rate).toBeNull();
    expect(ov.series).toEqual([{ month: '2026-10', income: 12000, spent: 0 }]);
    expect(ov.fx).toEqual({ base: 'MAD', source: 'fallback', fetched_at: null, unconverted: ['XYZ'] });
    expect(normalizeMoneyOverview(null, '2026-01').accounts).toEqual({ total: 0, items: [] });
  });

  it('formats rates and amounts', () => {
    expect(savingsLabel(null)).toBe('--');
    expect(savingsLabel(0.6249)).toBe('62%');
    expect(savingsLabel(-0.1)).toBe('-10%');
    expect(compactAmount(950.4)).toBe('950');
    expect(compactAmount(1250)).toBe('1.3k');
    expect(compactAmount(2000)).toBe('2k');
    expect(compactAmount(12400)).toBe('12k');
    expect(compactAmount(-3_500_000)).toBe('-3.5M');
  });

  it('parses typed amounts in French and English formats', () => {
    expect(parseAmount('45')).toBe(45);
    expect(parseAmount('30,5')).toBe(30.5);
    expect(parseAmount('1 200,50')).toBe(1200.5);
    expect(parseAmount('1,200.50')).toBe(1200.5);
    expect(parseAmount('1.200,50')).toBe(1200.5);
    expect(parseAmount('12,000')).toBe(12000);
    expect(parseAmount('-250')).toBe(-250);
    expect(parseAmount('12 000 DH')).toBe(12000);
    expect(parseAmount('')).toBeNull();
    expect(parseAmount('abc')).toBeNull();
  });

  it('scales cash-flow bars on the largest value', () => {
    expect(cashflowBars([{ income: 10000, spent: 5000 }, { income: 0, spent: 100 }], 100)).toEqual([
      { income: 100, spent: 50 },
      { income: 0, spent: 2 },
    ]);
    expect(cashflowBars([{ income: 0, spent: 0 }], 80)).toEqual([{ income: 0, spent: 0 }]);
  });

  it('labels months, ranges, FX and voice results', () => {
    expect(monthInitial('2026-10')).toBe('Oct');
    expect(monthRangeLabel('2026-10', null)).toBe('Since Oct 2026');
    expect(monthRangeLabel('2026-10', '2027-03')).toBe('Oct 2026 to Mar 2027');
    expect(fxLabel('fallback')).toMatch(/approximate/i);
    const e = { id: 'e' } as Expense;
    const i = { id: 'i' } as Income;
    expect(voiceMoneySummary([e, e], [i])).toBe('2 expenses and 1 income');
    expect(voiceMoneySummary([e], [])).toBe('1 expense');
    expect(voiceMoneySummary()).toBe('No amount heard');
  });
});

describe('bank SMS helpers', () => {
  it('builds the webhook URL with or without the query token', () => {
    expect(smsWebhookUrl('https://hub.example/', '/money/sms')).toBe('https://hub.example/money/sms');
    expect(smsWebhookUrl('https://edge.example/api', 'money/sms', 'a b&c')).toBe('https://edge.example/api/money/sms?token=a%20b%26c');
  });

  it('masks secrets', () => {
    expect(maskSecret('abcdefghijklmnopqrstuvwxyz')).toBe('abcd' + '•'.repeat(12));
    expect(maskSecret('abc')).toBe('••••');
  });

  it('labels what became of an SMS', () => {
    expect(smsOutcome({ status: 'created', kind: 'expense' })).toEqual({ label: 'Expense', tone: 'ok' });
    expect(smsOutcome({ status: 'created', kind: 'withdrawal' })).toEqual({ label: 'Moved to cash', tone: 'ok' });
    expect(smsOutcome({ status: 'ignored', kind: 'ignored', reason: 'one-time code' })).toEqual({ label: 'One-time code', tone: 'warn' });
    expect(smsOutcome({ status: 'ignored', kind: 'expense', ignored: 'same purchase as expense x' }).tone).toBe('neutral');
    expect(smsOutcome({ status: 'error', kind: null, reason: 'boom' })).toEqual({ label: 'boom', tone: 'bad' });
  });
});

describe('money client', () => {
  interface Call {
    method: string;
    url: URL;
    json?: unknown;
    form?: FormData;
  }

  function mockHub(reply: (call: Call) => { status?: number; body?: unknown }) {
    const calls: Call[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        const call: Call = {
          method: init.method ?? 'GET',
          url: new URL(url),
          json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
          form: init.body instanceof FormData ? init.body : undefined,
        };
        calls.push(call);
        const { status = 200, body } = reply(call);
        if (status === 204) return new Response(null, { status });
        return new Response(JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } });
      }),
    );
    return calls;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });

  it('calls every /money endpoint with the right method, path and body', async () => {
    const calls = mockHub((c) => {
      if (c.method === 'DELETE') return { status: 204 };
      if (c.url.pathname.endsWith('/money/overview')) return { body: { month: '2026-09', net: 5 } };
      if (c.url.pathname.endsWith('/money/voice')) return { status: 201, body: { transcript: 't', audio_key: 'k', expenses: [], incomes: [] } };
      return { body: { id: 'x' } };
    });
    const ov = await client.moneyOverview('2026-09');
    expect(ov.net).toBe(5);
    await client.voiceMoney(new Blob(['a']), { kind: 'income', filename: 'm.ogg' });
    await client.createIncome({ amount: 12000, source: 'Salaire', category: 'salary', recurring: true });
    await client.updateIncome('i 1', { end_month: '2026-12' });
    await client.deleteIncome('i1');
    await client.createRecurring({ name: 'Rent', amount: 3000 });
    await client.updateRecurring('r1', { amount: 3100 });
    await client.deleteRecurring('r1');
    await client.createAccount({ name: 'CIH', balance: 10 });
    await client.updateAccount('a1', { balance: 20 });
    await client.deleteAccount('a1');
    await client.setBudget('food', 1500);
    await client.deleteBudget('food');
    await client.smsSetup();
    await client.smsLog(5);
    await client.testSms('Achat 10 DH', 'CIH');

    expect(calls.map((c) => `${c.method} ${c.url.pathname}${c.url.search}`)).toEqual([
      'GET /api/money/overview?month=2026-09',
      'POST /api/money/voice',
      'POST /api/money/incomes',
      'PATCH /api/money/incomes/i%201',
      'DELETE /api/money/incomes/i1',
      'POST /api/money/recurring',
      'PATCH /api/money/recurring/r1',
      'DELETE /api/money/recurring/r1',
      'POST /api/money/accounts',
      'PATCH /api/money/accounts/a1',
      'DELETE /api/money/accounts/a1',
      'PUT /api/money/budgets/food',
      'DELETE /api/money/budgets/food',
      'GET /api/money/sms/setup',
      'GET /api/money/sms/log?limit=5',
      'POST /api/money/sms?dry_run=true',
    ]);
    expect(calls[15].json).toEqual({ from: 'CIH', text: 'Achat 10 DH' });
    expect(calls[1].form?.get('kind')).toBe('income');
    expect((calls[1].form?.get('file') as File).name).toBe('m.ogg');
    expect(calls[2].json).toEqual({ amount: 12000, source: 'Salaire', category: 'salary', recurring: true });
    expect(calls[11].json).toEqual({ amount: 1500, currency: 'MAD' });
  });
});
