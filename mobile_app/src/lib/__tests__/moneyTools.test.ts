import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient } from '../api';
import {
  allocateCents,
  autofillShares,
  computeShares,
  customSharesError,
  debtDetail,
  debtHeadline,
  equalShares,
  highlight,
  memberNames,
  myBalanceLabel,
  normaliseMerchant,
  normalizeDebts,
  pairLabel,
  percentShares,
  ruleActionLabel,
  ruleSuggestion,
  shareDebtLines,
  staleLent,
  voiceDebtLine,
  voiceDebtUndo,
  voiceDebtsOf,
} from '../moneyTools';
import type { Debt } from '../moneyTypes';
import type { ShareDraft } from '../moneyTools';

function debt(over: Partial<Debt> = {}): Debt {
  return {
    id: 'd1', person: 'Ayoub', direction: 'lent', amount: 200, currency: 'MAD', date: '2026-10-01', due_date: null, note: null,
    settled: false, settled_at: null, origin: 'manual', paid: 0, remaining: 200, overdue: false, age_days: 0, payments: [],
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', ...over,
  };
}

describe('merchant rules helpers', () => {
  it('normalises like the hub', () => {
    expect(normaliseMerchant('  MARJANE  Casa-Anfa 123 ')).toBe('marjane casa anfa 123');
    expect(normaliseMerchant('Café Bab')).toBe('cafe bab');
    expect(normaliseMerchant(null)).toBe('');
  });

  it('suggests a rule only when the category or the name changed', () => {
    expect(ruleSuggestion({ merchant: 'MARJANE 0042', category: 'other' }, { merchant: 'MARJANE 0042', category: 'groceries' })).toEqual({
      pattern: 'MARJANE 0042', category: 'groceries', rename: null,
    });
    expect(ruleSuggestion({ merchant: 'GLOVO*CASA', category: 'food' }, { merchant: 'Glovo', category: 'food' })).toEqual({
      pattern: 'GLOVO*CASA', category: null, rename: 'Glovo',
    });
    expect(ruleSuggestion({ merchant: 'Glovo', category: 'food' }, { merchant: 'glovo', category: 'food' })).toBeNull(); // same name
    expect(ruleSuggestion({ merchant: '', category: 'other' }, { merchant: 'X', category: 'food' })).toBeNull();
    expect(ruleSuggestion({ merchant: 'Taxi', category: 'transport' }, { merchant: 'Taxi', category: 'transport' })).toBeNull();
    expect(ruleActionLabel({ category: 'groceries', rename: 'Marjane' })).toBe('file under groceries, show as “Marjane”');
    expect(ruleActionLabel({ category: null, rename: null })).toBe('no action');
  });
});

describe('debts helpers', () => {
  it('normalises the listing and words the rows', () => {
    const l = normalizeDebts({ debts: [debt()], summary: { owed_to_me: 200, unconverted: ['XYZ', 3] } });
    expect(l.summary).toEqual({ currency: 'MAD', owed_to_me: 200, i_owe: 0, net: 0, by_person: [], unconverted: ['XYZ'] });
    expect(normalizeDebts(null).debts).toEqual([]);
    expect(debtHeadline(debt())).toBe('Ayoub owes you');
    expect(debtHeadline(debt({ direction: 'borrowed', person: 'Karim' }))).toBe('You owe Karim');
    expect(debtDetail(debt({ paid: 50, due_date: '2026-10-25' }), '2026-10-20')).toBe('Lent 19 days ago · 50 MAD back · due in 5 days');
    expect(debtDetail(debt({ date: '2026-10-20', due_date: '2026-10-19', direction: 'borrowed' }), '2026-10-20')).toBe('Borrowed today · 1 day late');
    expect(debtDetail(debt({ settled: true, due_date: '2026-10-19' }), '2026-10-20')).toBe('Lent 19 days ago · settled');
    const stale = staleLent([debt(), debt({ id: 'd2', date: '2026-10-15' }), debt({ id: 'd3', settled: true }), debt({ id: 'd4', direction: 'borrowed' })], '2026-10-20');
    expect(stale.map((d) => d.id)).toEqual(['d1']);
  });

  it('reads voice outcomes', () => {
    const out = voiceDebtsOf({ debts: [{ type: 'lend', action: 'created', person: 'Ayoub', amount: 200, currency: 'MAD', debts: [], applied: 0, unapplied: 0 }, { junk: 1 }] });
    expect(out).toHaveLength(1);
    expect(voiceDebtsOf({})).toEqual([]);
    expect(voiceDebtLine(out[0])).toBe('Lent to Ayoub');
    expect(voiceDebtLine({ type: 'repaid', action: 'repaid', person: 'Ayoub', unapplied: 30, currency: 'MAD' })).toBe('Ayoub paid you back (30 MAD more than owed)');
    expect(voiceDebtLine({ type: 'repay', action: 'repaid', person: 'Karim', unapplied: 0, currency: 'MAD' })).toBe('You paid Karim back');
    expect(voiceDebtLine({ type: 'borrow', action: 'created', person: 'Karim', unapplied: 0, currency: 'MAD' })).toBe('Borrowed from Karim');
    expect(voiceDebtLine({ type: 'repaid', action: 'unmatched', person: 'Sara', unapplied: 20, currency: 'MAD' })).toBe('No open loan with Sara (nothing recorded)');
    expect(voiceDebtLine({ type: 'lend', action: 'duplicate', person: 'Ayoub', unapplied: 0, currency: 'MAD' })).toBe('Already recorded a moment ago (Ayoub), not added twice');
  });
});

describe('split helpers', () => {
  it('splits in cents like the hub and checks custom shares', () => {
    expect(equalShares(100, ['a', 'b', 'c'])).toEqual({ a: 33.33, b: 33.33, c: 33.34 });
    expect(equalShares(10, [])).toEqual({});
    expect(customSharesError(60, { a: 10, b: 50 })).toBeNull();
    expect(customSharesError(60, { a: 10, b: 20 })).toBe('30.00 left to assign.');
    expect(customSharesError(60, { a: 50, b: 20 })).toBe('10.00 too much.');
    expect(customSharesError(60, { a: 0 })).toBe('Give at least one person a share.');
    expect(customSharesError(60, { a: -1, b: 61 })).toBe('Shares cannot be negative.');
    expect(memberNames([{ id: 'm', name: 'Me', me: true }, { id: 's', name: 'Sara', me: false }])).toEqual({ m: 'You', s: 'Sara' });
    expect(myBalanceLabel(120, 'MAD')).toBe('You are owed 120.00 MAD');
    expect(myBalanceLabel(-45.5, 'MAD')).toBe('You owe 45.50 MAD');
    expect(myBalanceLabel(0.001, 'MAD')).toBe('All settled');
  });
});

describe('share calculator', () => {
  const order = ['me', 'ay', 'sa'];
  const draft = (over: Partial<ShareDraft> = {}): ShareDraft => ({
    amount: 100, mode: 'equal', order, included: order, payer: 'me', amounts: {}, percents: {}, ...over,
  });

  it('rounds to the cent and gives the leftover cents to the payer, else the last member', () => {
    expect(allocateCents(10000, [['me', 1], ['ay', 1], ['sa', 1]], 'ay')).toEqual({ me: 3333, ay: 3334, sa: 3333 });
    expect(allocateCents(10000, [['me', 1], ['ay', 1], ['sa', 1]], 'zz')).toEqual({ me: 3333, ay: 3333, sa: 3334 });
    expect(allocateCents(2, [['me', 1], ['ay', 1], ['sa', 1]], null)).toEqual({ me: 0, ay: 0, sa: 2 });
    expect(allocateCents(500, [['me', 0], ['ay', 0]], 'me')).toEqual({});
    expect(equalShares(100, ['a', 'b', 'c'], 'b')).toEqual({ a: 33.33, b: 33.34, c: 33.33 });
    expect(equalShares(0.05, ['a', 'b'], 'a')).toEqual({ a: 0.03, b: 0.02 });
    expect(equalShares(10.01, ['a', 'b'])).toEqual({ a: 5, b: 5.01 });
    const shares = equalShares(1.115 * 3, ['a', 'b', 'c'], 'c');
    expect(Math.round(Object.values(shares).reduce((a, v) => a + v, 0) * 100)).toBe(Math.round(1.115 * 3 * 100));
  });

  it('turns percentages into amounts that add up exactly', () => {
    expect(percentShares(10, { me: 33.33, ay: 33.33, sa: 33.34 }, order, 'ay')).toEqual({ me: 3.33, ay: 3.34, sa: 3.33 });
    expect(percentShares(10, { me: 33.33, ay: 33.33, sa: 33.34 }, order, null)).toEqual({ me: 3.33, ay: 3.33, sa: 3.34 });
    expect(percentShares(200, { me: 70, ay: 30 }, order)).toEqual({ me: 140, ay: 60 });
    expect(percentShares(99.99, { me: 50, ay: 50 }, order, 'me')).toEqual({ me: 50, ay: 49.99 });
  });

  it('equal mode: only the included members, payer gets the cent', () => {
    expect(computeShares(draft({ payer: 'sa' }))).toEqual({ shares: { me: 33.33, ay: 33.33, sa: 33.34 }, remaining: 0, unit: null, error: null });
    expect(computeShares(draft({ included: ['me', 'sa'] })).shares).toEqual({ me: 50, sa: 50 });
    expect(computeShares(draft({ included: [] })).error).toBe('Pick who shares it.');
    expect(computeShares(draft({ amount: 0 })).error).toBe('Enter the amount.');
  });

  it('custom amounts: remaining counter and validation', () => {
    const half = computeShares(draft({ mode: 'custom', amounts: { me: 50, ay: 20 } }));
    expect(half).toMatchObject({ remaining: 30, unit: 'money', error: '30.00 left to assign.' });
    const over = computeShares(draft({ mode: 'custom', amounts: { me: 50, ay: 40, sa: 20 } }));
    expect(over).toMatchObject({ remaining: -10, error: '10.00 too much.' });
    // A left-out member's typed amount does not count.
    const ok = computeShares(draft({ mode: 'custom', included: ['me', 'ay'], amounts: { me: 50, ay: 50, sa: 99 } }));
    expect(ok).toEqual({ shares: { me: 50, ay: 50 }, remaining: 0, unit: 'money', error: null });
    expect(computeShares(draft({ mode: 'custom', amounts: { me: -1, ay: 101 } })).error).toBe('Shares cannot be negative.');
    expect(computeShares(draft({ mode: 'custom', amounts: {} })).error).toBe('Give at least one person a share.');
  });

  it('custom %: remaining in percent, amounts once it adds up', () => {
    const part = computeShares(draft({ mode: 'percent', amount: 10, percents: { me: 50, ay: 25 } }));
    expect(part).toMatchObject({ remaining: 25, unit: 'percent', error: '25.00 % left to assign.', shares: { me: 5, ay: 2.5 } });
    const full = computeShares(draft({ mode: 'percent', amount: 10, payer: 'ay', percents: { me: 33.33, ay: 33.33, sa: 33.34 } }));
    expect(full).toEqual({ shares: { me: 3.33, ay: 3.34, sa: 3.33 }, remaining: 0, unit: 'percent', error: null });
    expect(computeShares(draft({ mode: 'percent', percents: { me: 120 } })).error).toBe('Percentages go from 0 to 100.');
    expect(computeShares(draft({ mode: 'percent', percents: { me: 60, ay: 60 } })).error).toBe('20.00 % too much.');
  });

  it('auto-fills the remainder', () => {
    // Empty fields share what is left (cent to the payer).
    expect(autofillShares(draft({ mode: 'custom', amounts: { me: 40, ay: null, sa: null } }))).toEqual({ me: 40, ay: 30, sa: 30 });
    expect(autofillShares(draft({ mode: 'custom', amount: 10, payer: 'sa', amounts: { me: 0.01 } }))).toEqual({ me: 0.01, ay: 4.99, sa: 5 });
    // Nothing empty: on top of the payer, else the last included member.
    expect(autofillShares(draft({ mode: 'custom', amounts: { me: 40, ay: 30, sa: 20 } }))).toEqual({ me: 50, ay: 30, sa: 20 });
    expect(autofillShares(draft({ mode: 'custom', payer: 'zz', included: ['me', 'ay'], amounts: { me: 40, ay: 30 } }))).toEqual({ me: 40, ay: 60 });
    expect(autofillShares(draft({ mode: 'percent', percents: { me: 50 } }))).toEqual({ me: 50, ay: 25, sa: 25 });
    expect(autofillShares(draft({ mode: 'percent', percents: { me: 0, ay: 0, sa: 0 } }))).toEqual({ me: 33.34, ay: 33.33, sa: 33.33 });
    // Nothing to fill, or already too much, or equal mode.
    expect(autofillShares(draft({ mode: 'custom', amounts: { me: 100 } }))).toBeNull();
    expect(autofillShares(draft({ mode: 'custom', amounts: { me: 120 } }))).toBeNull();
    expect(autofillShares(draft())).toBeNull();
  });

  it('says what goes on the Debts page', () => {
    const names = { me: 'You', ay: 'Ayoub', sa: 'Sara' };
    expect(shareDebtLines({ me: 20, ay: 20, sa: 20 }, 'me', 'me', names)).toEqual(['Ayoub will owe you 20.00', 'Sara will owe you 20.00']);
    expect(shareDebtLines({ me: 45, sa: 45 }, 'sa', 'me', names)).toEqual(['You will owe Sara 45.00']);
    expect(shareDebtLines({ ay: 10, sa: 10 }, 'sa', 'me', names)).toEqual([]);
    expect(pairLabel({ name: 'Ayoub', net: 15 }, 'MAD')).toBe('Ayoub owes you 15.00 MAD');
    expect(pairLabel({ name: 'Sara', net: -45 }, 'MAD')).toBe('You owe Sara 45.00 MAD');
    expect(pairLabel({ name: 'Karim', net: 0.001 }, 'MAD')).toBe('All settled with Karim');
  });
});

describe('search helpers', () => {
  it('highlights every term, case-insensitive', () => {
    expect(highlight('Lait Centrale 2x LAIT', ['lait'])).toEqual([
      { text: 'Lait', hit: true }, { text: ' Centrale 2x ', hit: false }, { text: 'LAIT', hit: true },
    ]);
    expect(highlight('a (b)', ['(b'])).toEqual([{ text: 'a ', hit: false }, { text: '(b', hit: true }, { text: ')', hit: false }]);
    expect(highlight('abc', [])).toEqual([{ text: 'abc', hit: false }]);
    expect(highlight('', ['x'])).toEqual([]);
  });
});

// ------------------------------------------------------------------ client methods

interface Call {
  method: string;
  url: URL;
  json?: unknown;
  auth?: string;
}

function mockHub(reply: (call: Call) => { status?: number; body?: unknown; headers?: Record<string, string> }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      const call: Call = { method: init.method ?? 'GET', url: new URL(url), auth: headers.Authorization, json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined };
      calls.push(call);
      const r = reply(call);
      if (r.status === 204) return new Response(null, { status: 204 });
      return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { 'content-type': 'application/json', ...r.headers } });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });

describe('money tools client', () => {
  it('calls the rules, debts and splits endpoints', async () => {
    const calls = mockHub((c) => {
      if (c.url.pathname.endsWith('/money/rules') && c.method === 'GET') return { body: { rules: [{ id: 'r1' }] } };
      if (c.url.pathname.endsWith('/money/debts') && c.method === 'GET') return { body: { debts: [debt()], summary: { owed_to_me: 200 } } };
      if (c.url.pathname.endsWith('/money/splits') && c.method === 'GET') return { body: { groups: [{ id: 'g1' }] } };
      if (c.method === 'DELETE' && c.url.pathname.endsWith('/money/rules/r1')) return { status: 204 };
      return { body: { id: 'x', matched: 2, updated: 2 } };
    });
    expect((await client.merchantRules()).map((r) => r.id)).toEqual(['r1']);
    await client.saveMerchantRule({ pattern: 'MARJANE', category: 'groceries' });
    await client.applyMerchantRule('r1', true);
    await client.deleteMerchantRule('r1');
    expect((await client.debts('all')).summary.owed_to_me).toBe(200);
    await client.repayDebt('d1', { amount: 50 });
    await client.deleteDebtPayment('d1', 'p/1');
    expect((await client.splitGroups()).map((g) => g.id)).toEqual(['g1']);
    await client.addSplitEntry('g1', { description: 'Riad', amount: 300, paid_by: 'm', split: 'equal' });
    await client.settleSplit('g1', { from: 's', to: 'm', amount: 45 });
    expect(calls.map((c) => `${c.method} ${c.url.pathname}${c.url.search}`)).toEqual([
      'GET /api/money/rules',
      'POST /api/money/rules',
      'POST /api/money/rules/r1/apply?dry_run=1',
      'DELETE /api/money/rules/r1',
      'GET /api/money/debts?status=all',
      'POST /api/money/debts/d1/repay',
      'DELETE /api/money/debts/d1/payments/p%2F1',
      'GET /api/money/splits',
      'POST /api/money/splits/g1/expenses',
      'POST /api/money/splits/g1/settle',
    ]);
    expect(calls[1].json).toEqual({ pattern: 'MARJANE', category: 'groceries' });
    expect(calls[9].json).toEqual({ from: 's', to: 'm', amount: 45 });
    expect(calls.every((c) => c.auth === 'Bearer tok')).toBe(true);
  });

  it('searches expenses with the bearer token', async () => {
    const calls = mockHub(() => ({ body: { q: 'lait', terms: ['lait'], results: [{ id: 'e1', receipt_url: 'https://s/x' }] } }));
    const found = await client.searchExpenses('lait', { receiptsOnly: true });
    expect(found.count).toBe(1);
    expect(found.terms).toEqual(['lait']);
    expect(calls[0].url.pathname).toBe('/api/expenses/search');
    expect(calls[0].url.search).toBe('?q=lait&receipts_only=1');
    expect(calls[0].auth).toBe('Bearer tok');
  });
});

describe('voice debt undo', () => {
  it('removes the loans created and the repayments recorded, nothing for duplicates or unmatched', () => {
    const ops = voiceDebtUndo([
      { type: 'lend', action: 'created', person: 'Ayoub', amount: 200, currency: 'MAD', debts: [debt({ id: 'new' })], payment_ids: [], applied: 0, unapplied: 0 },
      { type: 'repaid', action: 'repaid', person: 'Karim', amount: 250, currency: 'MAD', debts: [debt({ id: 'k1' }), debt({ id: 'k2' })], payment_ids: ['p1', 'p2'], applied: 250, unapplied: 0 },
      { type: 'lend', action: 'duplicate', person: 'Ayoub', amount: 200, currency: 'MAD', debts: [debt({ id: 'old' })], payment_ids: [], applied: 0, unapplied: 0 },
      { type: 'repaid', action: 'unmatched', person: 'Sara', amount: 20, currency: 'MAD', debts: [], applied: 0, unapplied: 20 },
      { type: 'repay', action: 'repaid', person: 'Omar', amount: 5, currency: 'MAD', debts: [debt({ id: 'o1' })], applied: 5, unapplied: 0 },
      {
        type: 'split', action: 'created', person: 'Ayoub, Sara', amount: 300, currency: 'MAD', debts: [debt({ id: 'linked', origin: 'split' })],
        payment_ids: [], applied: 0, unapplied: 0, split: { group_id: 'g1', group_name: 'Dîner', entry_id: 'e1', group_created: true, my_share: 100 },
      },
      { type: 'split', action: 'duplicate', person: 'Ayoub', amount: 300, currency: 'MAD', debts: [], applied: 0, unapplied: 0 },
    ]);
    expect(ops).toEqual([
      { kind: 'debt', id: 'new' },
      { kind: 'payment', debtId: 'k1', paymentId: 'p1' },
      { kind: 'payment', debtId: 'k2', paymentId: 'p2' },
      { kind: 'split', groupId: 'g1', entryId: 'e1', groupCreated: true },
    ]);
    expect(
      voiceDebtLine({ type: 'split', action: 'created', person: 'Ayoub, Sara', unapplied: 0, currency: 'MAD', split: { group_id: 'g1', group_name: 'Dîner', entry_id: 'e1', group_created: true, my_share: 100 } }),
    ).toBe('Shared with Ayoub, Sara in Dîner (new group)');
    expect(voiceDebtUndo([])).toEqual([]);
  });
});
