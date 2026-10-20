import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient, normalizeDigest, normalizeSyncResult } from '../api';
import { ago, emailMailbox, gmailSyncTemplate, headlineStats, providerBars, rangeLabel, scheduleHeaders, skipReasons, syncSummary, taskShares } from '../digest';
import type { Expense } from '../types';

// Gmail receipts import (/inbox/receipts/*) and the summary (/digest), mocked at fetch, plus their helpers.

interface Call {
  method: string;
  url: URL;
  json?: unknown;
  auth?: string;
}

function mockHub(reply: (call: Call) => { status?: number; body?: unknown }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      const call: Call = {
        method: init.method ?? 'GET',
        url: new URL(url),
        auth: headers.Authorization,
        json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
      };
      calls.push(call);
      const { status = 200, body } = reply(call);
      return new Response(JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });

const emailExpense: Expense = {
  id: 'e9',
  date: '2026-10-17',
  merchant: 'Jumia',
  total: 450,
  currency: 'MAD',
  category: 'shopping',
  items: [],
  note: 'Email: Votre facture',
  receipt_key: 'receipts/email/2026/10/x.pdf',
  source: 'email',
  source_ref: 'simaxhah@gmail.com#pdf-1@shop.ma',
  created_at: '2026-10-20T08:00:00Z',
};

const digestBody = {
  period: 'week',
  from: '2026-10-14',
  to: '2026-10-20',
  generated_at: '2026-10-20T08:00:00Z',
  markdown: '## Your week',
  stats: {
    expenses: {
      total_by_currency: { MAD: 126, EUR: 80 },
      by_category: { groceries: { MAD: 120 }, transport: { MAD: 6 }, travel: { EUR: 80 } },
      count: 4,
      top_merchants: [{ merchant: 'Marjane', currency: 'MAD', total: 120, count: 2 }],
    },
    tasks: { done: 3, open: 4, overdue: 1 },
    notes: { created: 2, voice: 1 },
    drive: { uploaded_files: 5, uploaded_bytes: 2048 },
    llm: { requests_by_provider: { groq: 9, gemini: 3 } },
  },
  highlights: ['Groceries led spending'],
  suggestions: ['Clear the overdue task'],
  source: 'llm',
  cached: true,
};

describe('gmail import endpoints', () => {
  it('reads the status', async () => {
    const account = { name: 'work', user: 'iouhnine@bidtndr.com', host: 'imap.zoho.eu', port: 993, password_key: 'INBOX_PW_WORK', configured: true, last_sync_at: null, last_error: 'login failed' };
    const calls = mockHub(() => ({ body: { configured: true, accounts: [account, 'junk'], problems: ['bad entry'], last_sync_at: '2026-10-20T07:00:00Z', last_result: { matched: 2 } } }));
    const s = await client.inboxStatus();
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url.pathname).toBe('/api/inbox/receipts/status');
    expect(calls[0].auth).toBe('Bearer tok');
    expect(s).toEqual({
      configured: true,
      accounts: [{ name: 'work', user: 'iouhnine@bidtndr.com', host: 'imap.zoho.eu', port: 993, configured: true, last_sync_at: null, last_error: 'login failed' }],
      problems: ['bad entry'],
      last_sync_at: '2026-10-20T07:00:00Z',
      last_result: { matched: 2 },
    });
  });

  it('runs a sync and normalises the reply', async () => {
    const calls = mockHub(() => ({
      body: { scanned: 5, matched: 1, created: [emailExpense], skipped: [{ message_id: 'n@x', reason: 'not a receipt' }], errors: [], remaining: 2 },
    }));
    const r = await client.syncGmailReceipts({ days: 3, dry_run: true, account: 'work' });
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url.pathname).toBe('/api/inbox/receipts/sync');
    expect(calls[0].json).toEqual({ days: 3, dry_run: true, account: 'work' });
    expect(r.accounts).toEqual([]);
    expect(r.created[0].source).toBe('email');
    expect(r.remaining).toBe(2);
    expect(r.dry_run).toBe(false);
  });

  it('surfaces "not configured" as an error', async () => {
    mockHub(() => ({ status: 409, body: { detail: 'Gmail is not configured: store GMAIL_IMAP_USER and GMAIL_APP_PASSWORD' } }));
    await expect(client.syncGmailReceipts()).rejects.toThrow(/Gmail is not configured/);
  });

  it('fills missing fields with safe defaults', () => {
    expect(normalizeSyncResult(null)).toEqual({ scanned: 0, matched: 0, created: [], skipped: [], errors: [], remaining: 0, dry_run: false, accounts: [] });
  });

  it('reads the mailbox of an imported expense', () => {
    expect(emailMailbox('simaxhah@gmail.com#pdf-1@shop.ma')).toBe('simaxhah@gmail.com');
    expect(emailMailbox('pdf-1@shop.ma')).toBeNull();
    expect(emailMailbox(null)).toBeNull();
  });
});

describe('digest endpoint', () => {
  it('asks for a period and forces a refresh only when told to', async () => {
    const calls = mockHub(() => ({ body: digestBody }));
    const d = await client.digest('month');
    expect(calls[0].url.pathname).toBe('/api/digest');
    expect(calls[0].url.searchParams.get('period')).toBe('month');
    expect(calls[0].url.searchParams.has('refresh')).toBe(false);
    expect(d.stats.tasks).toEqual({ done: 3, open: 4, overdue: 1 });
    expect(d.cached).toBe(true);
    await client.digest('week', true);
    expect(calls[1].url.searchParams.get('refresh')).toBe('1');
  });

  it('normalises a partial reply', () => {
    const d = normalizeDigest({ markdown: 'hi', stats: { tasks: { done: 2 } }, highlights: ['a', 3] }, 'month');
    expect(d.period).toBe('month');
    expect(d.stats.tasks).toEqual({ done: 2, open: 0, overdue: 0 });
    expect(d.stats.drive).toEqual({ uploaded_files: null, uploaded_bytes: null });
    expect(d.stats.expenses.top_merchants).toEqual([]);
    expect(d.highlights).toEqual(['a']);
  });
});

describe('summary helpers', () => {
  const d = normalizeDigest(digestBody);

  it('picks the Home card numbers', () => {
    expect(headlineStats(d)).toEqual([
      { label: 'Spent', value: expect.stringContaining('126') },
      { label: 'Tasks done', value: '3' },
      { label: 'Notes', value: '2' },
    ]);
    const quiet = normalizeDigest({ ...digestBody, stats: { ...digestBody.stats, expenses: {}, notes: { created: 0 } } });
    expect(headlineStats(quiet)[0].value).toBe('Nothing');
    expect(headlineStats(quiet)[2]).toEqual({ label: 'Uploads', value: '5' });
  });

  it('splits tasks and ranks providers', () => {
    const s = taskShares({ done: 3, open: 4, overdue: 1 });
    expect(s.done + s.open + s.overdue).toBeCloseTo(1);
    expect(s.overdue).toBeCloseTo(1 / 7);
    expect(taskShares({ done: 0, open: 0, overdue: 0 })).toEqual({ done: 0, open: 0, overdue: 0 });
    expect(providerBars({ gemini: 3, groq: 9, idle: 0 })).toEqual([
      { provider: 'groq', count: 9, share: 1 },
      { provider: 'gemini', count: 3, share: 1 / 3 },
    ]);
  });

  it('labels ranges and ages', () => {
    expect(rangeLabel('2026-10-14', '2026-10-20')).toBe('14 Oct – 20 Oct');
    expect(rangeLabel('2026-10-20', '2026-10-20')).toBe('20 Oct');
    const now = new Date('2026-10-20T10:00:00Z');
    expect(ago('2026-10-20T09:59:30Z', now)).toBe('just now');
    expect(ago('2026-10-20T09:48:00Z', now)).toBe('12 min ago');
    expect(ago('2026-10-20T07:00:00Z', now)).toBe('3 h ago');
    expect(ago(null, now)).toBe('');
  });

  it('summarises a sync', () => {
    const r = normalizeSyncResult({
      created: [emailExpense, emailExpense],
      skipped: [
        { message_id: 'a', reason: 'already imported' },
        { message_id: 'b', reason: 'not a receipt' },
        { message_id: 'c', reason: 'already imported' },
      ],
      errors: [{ message_id: 'd', error: 'timed out' }],
      remaining: 3,
    });
    expect(syncSummary(r)).toBe('2 new expenses, 3 skipped, 1 failed, 3 left for next run');
    expect(skipReasons(r)).toEqual([
      { reason: 'not a receipt', count: 1 },
      { reason: 'already imported', count: 2 },
    ]);
    expect(syncSummary(normalizeSyncResult({}))).toBe('No new receipts');
  });

  it('builds the daily Gmail schedule', () => {
    expect(gmailSyncTemplate('https://api.ilyasouhnine.tech/').target).toBe('https://api.ilyasouhnine.tech/inbox/receipts/sync');
    expect(scheduleHeaders({ body: '{}', token: 'tok', withToken: true })).toEqual({ 'Content-Type': 'application/json', Authorization: 'Bearer tok' });
    expect(scheduleHeaders({ body: null, token: 'tok', withToken: false })).toBeUndefined();
  });
});
