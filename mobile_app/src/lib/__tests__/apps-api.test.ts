import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient } from '../api';

// Every round-3 app endpoint (notes, tasks, expenses, links, drops, rag, budget), mocked at fetch.

interface Call {
  method: string;
  url: URL;
  json?: unknown;
  form?: FormData;
  auth?: string;
}

function mockHub(reply: (call: Call) => { status?: number; body?: unknown }) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    const headers = init.headers as Record<string, string>;
    const call: Call = {
      method: init.method ?? 'GET',
      url: new URL(url),
      auth: headers.Authorization,
      json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
      form: init.body instanceof FormData ? init.body : undefined,
    };
    calls.push(call);
    const { status = 200, body } = reply(call);
    if (status === 204) return new Response(null, { status });
    return new Response(JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });

const note = {
  id: 'n1',
  title: 'Groceries',
  body: '- milk',
  notebook: 'Inbox',
  tags: ['home'],
  source: 'text',
  audio_key: null,
  pinned: false,
  created_at: '2026-10-20T08:00:00Z',
  updated_at: '2026-10-20T08:00:00Z',
};
const task = {
  id: 't1',
  title: 'Call bank',
  details: null,
  due: '2026-10-21',
  done: false,
  priority: 'high',
  note_id: null,
  created_at: '2026-10-20T08:00:00Z',
  done_at: null,
};
const expense = {
  id: 'e1',
  date: '2026-10-19',
  merchant: 'Marjane',
  total: 245.5,
  currency: 'MAD',
  category: 'groceries',
  items: [{ name: 'bread', qty: 2, price: 3 }],
  note: null,
  receipt_key: 'receipts/2026/10/e1.jpg',
  created_at: '2026-10-19T18:00:00Z',
};

describe('notes', () => {
  it('lists notes with notebook and search filters', async () => {
    const calls = mockHub(() => ({ body: { notes: [note], notebooks: [{ name: 'Inbox', count: 1 }] } }));
    const res = await client.notes({ notebook: 'Inbox', q: ' milk ', limit: 50 });
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url.pathname).toBe('/api/notes');
    expect(Object.fromEntries(calls[0].url.searchParams)).toEqual({ notebook: 'Inbox', q: 'milk', limit: '50' });
    expect(calls[0].auth).toBe('Bearer tok');
    expect(res.notes[0].id).toBe('n1');
    expect(res.notebooks).toEqual([{ name: 'Inbox', count: 1 }]);
  });

  it('omits empty filters', async () => {
    const calls = mockHub(() => ({ body: { notes: [], notebooks: [] } }));
    await client.notes({ q: '  ' });
    expect(calls[0].url.search).toBe('');
  });

  it('creates, reads, patches and deletes a note', async () => {
    const calls = mockHub((c) => (c.method === 'DELETE' ? { status: 204 } : { status: c.method === 'POST' ? 201 : 200, body: note }));
    expect((await client.createNote({ body: '- milk', tags: ['home'] })).id).toBe('n1');
    expect(calls[0]).toMatchObject({ method: 'POST', json: { body: '- milk', tags: ['home'] } });
    await client.note('n1');
    expect(calls[1].url.pathname).toBe('/api/notes/n1');
    await client.updateNote('n1', { pinned: true });
    expect(calls[2]).toMatchObject({ method: 'PATCH', json: { pinned: true } });
    expect(await client.deleteNote('n1')).toBeUndefined();
    expect(calls[3].method).toBe('DELETE');
  });

  it('uploads a voice note as multipart with the mode', async () => {
    const calls = mockHub(() => ({ status: 201, body: { transcript: 'buy milk', note, tasks: [task], audio_key: 'voice/2026/10/x.webm' } }));
    const res = await client.voiceNote(new Blob(['abc'], { type: 'audio/webm' }), { mode: 'both', notebook: 'Inbox', filename: 'v.webm' });
    expect(calls[0].url.pathname).toBe('/api/notes/voice');
    expect(calls[0].form?.get('mode')).toBe('both');
    expect(calls[0].form?.get('notebook')).toBe('Inbox');
    expect((calls[0].form?.get('file') as File).name).toBe('v.webm');
    expect(res.tasks).toHaveLength(1);
    expect(res.audio_key).toContain('voice/');
  });

  it('surfaces a 422 for an empty transcript', async () => {
    mockHub(() => ({ status: 422, body: { detail: 'no speech detected' } }));
    await expect(client.voiceNote(new Blob(['a']))).rejects.toMatchObject({ status: 422, message: 'no speech detected' });
  });

  it('defaults the voice mode to auto', async () => {
    const calls = mockHub(() => ({ status: 201, body: { transcript: '', note: null, tasks: [], audio_key: 'k' } }));
    await client.voiceNote(new Blob(['a']));
    expect(calls[0].form?.get('mode')).toBe('auto');
    expect(calls[0].form?.has('notebook')).toBe(false);
  });
});

describe('tasks', () => {
  it('lists, creates, patches and deletes tasks', async () => {
    const calls = mockHub((c) => {
      if (c.method === 'GET') return { body: { tasks: [task] } };
      if (c.method === 'DELETE') return { status: 204 };
      return { body: { ...task, ...(c.json as object) } };
    });
    const list = await client.tasks({ status: 'open', due_before: '2026-10-31' });
    expect(Object.fromEntries(calls[0].url.searchParams)).toEqual({ status: 'open', due_before: '2026-10-31' });
    expect(list[0].priority).toBe('high');
    await client.createTask({ title: 'Call bank', due: '2026-10-21', priority: 'high' });
    expect(calls[1]).toMatchObject({ method: 'POST', json: { title: 'Call bank', due: '2026-10-21', priority: 'high' } });
    expect(calls[1].url.pathname).toBe('/api/tasks');
    const done = await client.updateTask('t1', { done: true });
    expect(done.done).toBe(true);
    expect(calls[2].url.pathname).toBe('/api/tasks/t1');
    await client.deleteTask('t1');
    expect(calls[3].method).toBe('DELETE');
  });
});

describe('expenses', () => {
  it('scans a receipt photo', async () => {
    const calls = mockHub(() => ({ status: 201, body: { expense, confidence: 'medium' } }));
    const res = await client.scanReceipt(new Blob(['jpg'], { type: 'image/jpeg' }), { note: 'lunch' });
    expect(calls[0].url.pathname).toBe('/api/expenses/receipt');
    expect(calls[0].form?.get('note')).toBe('lunch');
    expect((calls[0].form?.get('file') as File).name).toBe('receipt.jpg');
    expect(res.confidence).toBe('medium');
  });

  it('lists a month with its summary, tolerating a missing summary', async () => {
    const summary = { total_by_currency: { MAD: 245.5 }, by_category: { groceries: { MAD: 245.5 } }, count: 1 };
    let calls = mockHub(() => ({ body: { expenses: [expense], summary } }));
    const res = await client.expenses('2026-10');
    expect(calls[0].url.searchParams.get('month')).toBe('2026-10');
    expect(res.summary).toEqual(summary);
    calls = mockHub(() => ({ body: { expenses: [] } }));
    expect((await client.expenses()).summary).toEqual({ total_by_currency: {}, by_category: {}, count: 0 });
    expect(calls[0].url.search).toBe('');
  });

  it('creates, patches and deletes an expense', async () => {
    const calls = mockHub((c) => (c.method === 'DELETE' ? { status: 204 } : { body: expense }));
    const { id: _id, created_at: _c, ...fields } = expense;
    await client.createExpense(fields as never);
    expect(calls[0]).toMatchObject({ method: 'POST', json: { merchant: 'Marjane', total: 245.5 } });
    await client.updateExpense('e1', { category: 'food' });
    expect(calls[1]).toMatchObject({ method: 'PATCH', json: { category: 'food' } });
    expect(calls[1].url.pathname).toBe('/api/expenses/e1');
    await client.deleteExpense('e1');
    expect(calls[2].method).toBe('DELETE');
    expect((await client.expense('e1')).merchant).toBe('Marjane');
    expect(calls[3]).toMatchObject({ method: 'GET' });
    expect(calls[3].url.pathname).toBe('/api/expenses/e1');
  });
});

describe('links and drops', () => {
  const link = { code: 'abc123', url: 'https://example.com', title: null, clicks: 3, created_at: '', expires_at: null, short_url: 'https://api.x/s/abc123' };
  const drop = {
    code: 'd1',
    provider: 'cloudflare_r2',
    key: 'docs/a b.pdf',
    filename: 'a b.pdf',
    size: 10,
    created_at: '',
    expires_at: '',
    max_downloads: 5,
    downloads: 0,
    has_password: true,
    url: 'https://api.x/d/d1',
  };

  it('manages short links', async () => {
    const calls = mockHub((c) => (c.method === 'GET' ? { body: { links: [link] } } : c.method === 'DELETE' ? { status: 204 } : { status: 201, body: link }));
    expect((await client.links())[0].clicks).toBe(3);
    await client.createLink({ url: 'https://example.com', code: 'abc123', expires_in_days: 7 });
    expect(calls[1]).toMatchObject({ method: 'POST', json: { url: 'https://example.com', code: 'abc123', expires_in_days: 7 } });
    await client.deleteLink('abc/123');
    expect(calls[2].url.pathname).toBe('/api/links/abc%2F123');
  });

  it('surfaces a 409 when the code is taken', async () => {
    mockHub(() => ({ status: 409, body: { detail: 'code taken' } }));
    await expect(client.createLink({ url: 'https://x.y', code: 'abc123' })).rejects.toMatchObject({ status: 409, message: 'code taken' });
  });

  it('manages drops', async () => {
    const calls = mockHub((c) => (c.method === 'GET' ? { body: { drops: [drop] } } : c.method === 'DELETE' ? { status: 204 } : { status: 201, body: drop }));
    expect((await client.drops())[0].has_password).toBe(true);
    await client.createDrop({ provider: 'cloudflare_r2', key: 'docs/a b.pdf', expires_in_hours: 24, max_downloads: 5, password: 'pw' });
    expect(calls[1].url.pathname).toBe('/api/drops');
    expect(calls[1].json).toEqual({ provider: 'cloudflare_r2', key: 'docs/a b.pdf', expires_in_hours: 24, max_downloads: 5, password: 'pw' });
    await client.deleteDrop('d1');
    expect(calls[2]).toMatchObject({ method: 'DELETE' });
    expect(calls[2].url.pathname).toBe('/api/drops/d1');
  });
});

describe('ask my drive', () => {
  const doc = { provider: 'cloudflare_r2', key: 'docs/a b.pdf', title: 'a b', mime: 'application/pdf', size: 10, chunks: 4, indexed_at: '', error: null };

  it('lists docs and the embedding model', async () => {
    mockHub(() => ({ body: { docs: [doc], embedding_model: 'bge-m3' } }));
    const res = await client.ragDocs();
    expect(res.docs[0].chunks).toBe(4);
    expect(res.embedding_model).toBe('bge-m3');
  });

  it('indexes one file per call and deletes by provider/key path', async () => {
    const calls = mockHub((c) => (c.method === 'DELETE' ? { status: 204 } : { body: { doc } }));
    expect((await client.ragIndex('cloudflare_r2', 'docs/a b.pdf')).title).toBe('a b');
    expect(calls[0]).toMatchObject({ method: 'POST', json: { provider: 'cloudflare_r2', key: 'docs/a b.pdf' } });
    expect(calls[0].url.pathname).toBe('/api/rag/index');
    await client.ragDelete('cloudflare_r2', 'docs/a b.pdf');
    expect(calls[1].url.pathname).toBe('/api/rag/docs/cloudflare_r2/docs/a%20b.pdf');
  });

  it('maps a 404 for a doc that was never indexed', async () => {
    mockHub(() => ({ status: 404, body: { detail: 'not indexed' } }));
    await expect(client.ragDelete('p', 'k.pdf')).rejects.toMatchObject({ status: 404 });
  });

  it('asks a question', async () => {
    const answer = { answer: 'Yes [1].', sources: [{ n: 1, provider: 'p', key: 'k', title: 't', page: 2, snippet: 's', score: 0.9 }], routed_via: 'groq' };
    const calls = mockHub(() => ({ body: answer }));
    const res = await client.ragAsk({ question: 'Is it?', top_k: 8 });
    expect(calls[0].json).toEqual({ question: 'Is it?', top_k: 8 });
    expect(res.sources[0].page).toBe(2);
  });
});

describe('budget', () => {
  it('reads items and normalises the summary', async () => {
    const item = {
      provider: 'groq',
      kind: 'llm',
      group: 'llm',
      metric: 'requests',
      window: 'day',
      used: 900,
      limit: 1000,
      unit: 'requests',
      fraction: 0.9,
      status: 'warn',
      reset_at: null,
      source: 'router',
    };
    const calls = mockHub(() => ({ body: { generated_at: '2026-10-20T08:00:00Z', items: [item], summary: { ok: 2, warn: 1 } } }));
    const res = await client.budget();
    expect(calls[0].url.pathname).toBe('/api/budget');
    expect(res.items[0].status).toBe('warn');
    expect(res.summary).toEqual({ ok: 2, warn: 1, over: 0, unknown: 0 });
  });
});
