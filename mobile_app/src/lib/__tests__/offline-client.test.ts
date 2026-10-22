import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HubError } from '../api';
import { OfflineHubClient, isQueuedVoice } from '../offline/client';
import { OfflineManager } from '../offline/manager';
import { isPending } from '../offline/model';
import { IdbStore } from '../offline/store';

// The offline-aware client end to end: a fake hub behind fetch (that can go down), fake IndexedDB.

interface Call {
  method: string;
  path: string;
  json?: Record<string, unknown>;
  form?: FormData;
}

type Reply = (call: Call) => { status?: number; body?: unknown } | 'hang';

let down = false;
let calls: Call[] = [];
let reply: Reply = () => ({ body: {} });

beforeEach(() => {
  down = false;
  calls = [];
  reply = () => ({ body: {} });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (down) throw new TypeError('Failed to fetch');
      const call: Call = {
        method: init.method ?? 'GET',
        path: new URL(url).pathname + new URL(url).search,
        json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
        form: init.body instanceof FormData ? init.body : undefined,
      };
      calls.push(call);
      const r = reply(call);
      if (r === 'hang') return new Promise<Response>(() => undefined);
      const { status = 200, body } = r;
      if (status === 204) return new Response(null, { status });
      return new Response(JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function setup(timeoutMs = 5_000) {
  const manager = new OfflineManager(new IdbStore(new IDBFactory(), `c-${Math.random()}`), () => true, () => Date.UTC(2026, 9, 22, 10, 0));
  const client = new OfflineHubClient({ hubUrl: 'https://hub.example', token: 'tok' }, manager, timeoutMs);
  return { manager, client };
}

const task = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
  id, title, details: null, due: null, due_time: null, done: false, priority: 'normal', note_id: null,
  created_at: '2026-10-20T08:00:00Z', done_at: null, ...extra,
});

/** Lets the background replay started by a queued write settle. */
async function settle(manager: OfflineManager) {
  await vi.waitFor(async () => expect(manager.state.syncing).toBe(false));
  await manager.sync();
}

describe('read cache', () => {
  it('serves the last good list when the hub is unreachable', async () => {
    const { client, manager } = setup();
    reply = () => ({ body: { tasks: [task('a', 'Live')] } });
    expect((await client.tasks({ status: 'open' })).map((t) => t.title)).toEqual(['Live']);
    expect(manager.state.cachedAt).toBeNull();

    down = true;
    expect((await client.tasks({ status: 'open' })).map((t) => t.title)).toEqual(['Live']);
    expect(manager.state).toMatchObject({ online: false, cachedAt: Date.UTC(2026, 9, 22, 10, 0) });

    // Another query was never loaded online: the network error surfaces.
    await expect(client.tasks({ status: 'done' })).rejects.toMatchObject({ status: 0 });
    // Endpoints outside the cache are untouched.
    await expect(client.links()).rejects.toBeInstanceOf(HubError);

    down = false;
    const refreshed = vi.fn();
    manager.onRefresh(refreshed);
    // Known out of reach: the saved copy answers at once, the live answer arrives in the background.
    expect((await client.tasks({ status: 'open' })).map((t) => t.title)).toEqual(['Live']);
    await vi.waitFor(() => expect(manager.state).toMatchObject({ online: true, cachedAt: null }));
    expect(refreshed).toHaveBeenCalled();
  });

  it('never serves the saved data of another hub', async () => {
    const { client, manager } = setup();
    reply = () => ({ body: { tasks: [task('a', 'From A')] } });
    await client.tasks({ status: 'open' });
    const other = new OfflineHubClient({ hubUrl: 'https://other.example', token: 'tok2' }, manager, 5_000);
    down = true;
    await expect(other.tasks({ status: 'open' })).rejects.toMatchObject({ status: 0 });
  });

  it('does not wait for the timeout again once the hub is known to be out of reach', async () => {
    const { client, manager } = setup(5_000);
    reply = () => ({ body: { tasks: [task('a', 'Saved')] } });
    await client.tasks({ status: 'open' });
    manager.unreachable();
    reply = () => 'hang'; // captive portal: the browser says online, nothing answers
    const started = Date.now();
    expect((await client.tasks({ status: 'open' })).map((t) => t.title)).toEqual(['Saved']);
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it('caches notes, opened notes, expenses, the money overview, the agenda and chats', async () => {
    const { client } = setup();
    reply = (c) => {
      if (c.path.startsWith('/notes/n1')) return { body: { id: 'n1', title: 'Opened', body: 'x' } };
      if (c.path.startsWith('/notes')) return { body: { notes: [{ id: 'n1', title: 'Opened' }], notebooks: [{ name: 'Inbox', count: 1 }] } };
      if (c.path.startsWith('/expenses')) return { body: { expenses: [{ id: 'e1', date: '2026-10-02', total: 5, currency: 'MAD', category: 'food' }], summary: { total_by_currency: { MAD: 5 }, by_category: {}, count: 1 } } };
      if (c.path.startsWith('/money/overview')) return { body: { month: '2026-10', income: { total: 100, items: [] } } };
      if (c.path.startsWith('/gcal/events')) return { body: { configured: true, events: [{ id: 'g1', title: 'Dentist' }] } };
      if (c.path.startsWith('/chats')) return { body: { chats: [{ id: 'c1', title: 'Ask' }] } };
      return { body: {} };
    };
    const live = await Promise.all([client.notes({ limit: 100 }), client.note('n1'), client.expenses('2026-10'), client.moneyOverview('2026-10'), client.gcalEvents(7), client.chatList({ limit: 200 })]);
    down = true;
    const saved = await Promise.all([client.notes({ limit: 100 }), client.note('n1'), client.expenses('2026-10'), client.moneyOverview('2026-10'), client.gcalEvents(7), client.chatList({ limit: 200 })]);
    expect(saved).toEqual(live);
  });

  it('falls back to the saved copy when the hub hangs', async () => {
    const { client, manager } = setup(30);
    reply = () => ({ body: { tasks: [task('a', 'Saved')] } });
    await client.tasks();
    reply = () => 'hang';
    expect((await client.tasks()).map((t) => t.title)).toEqual(['Saved']);
    expect(manager.state.cachedAt).not.toBeNull();
  });

  it('lets hub errors through', async () => {
    const { client } = setup();
    reply = () => ({ status: 500, body: { detail: 'boom' } });
    await expect(client.tasks()).rejects.toMatchObject({ status: 500, message: 'boom' });
  });
});

describe('write queue', () => {
  it('sends creates online with a client id', async () => {
    const { client } = setup();
    reply = (c) => ({ status: 201, body: task(String(c.json?.id), 'Buy milk') });
    const t = await client.createTask({ title: 'Buy milk' });
    expect(calls[0]).toMatchObject({ method: 'POST', path: '/tasks', json: { title: 'Buy milk', id: t.id } });
    expect(t.id).toMatch(/^o[0-9A-Za-z]{15}$/);
    expect(isPending(t)).toBe(false);
  });

  it('queues writes offline, shows them, and replays them once', async () => {
    const { client, manager } = setup();
    reply = () => ({ body: { tasks: [task('srv', 'From server')] } });
    await client.tasks({ status: 'all' });

    down = true;
    const created = await client.createTask({ title: 'Offline task', priority: 'high' });
    expect(created).toMatchObject({ title: 'Offline task', priority: 'high', done: false });
    expect(isPending(created)).toBe(true);
    const toggled = await client.updateTask('srv', { done: true });
    expect(toggled).toMatchObject({ id: 'srv', title: 'From server', done: true }); // merged on the row seen before
    expect(isPending(toggled)).toBe(true);
    expect(manager.state.pending).toBe(2);
    await settle(manager); // the replay attempts fail while the hub is down

    const shown = await client.tasks({ status: 'all' });
    expect(shown.map((t) => [t.title, t.done, isPending(t)])).toEqual([['From server', true, true], ['Offline task', false, true]]);

    // Back online: replayed in order, the create carries the same id (the hub dedupes on it).
    down = false;
    calls = [];
    reply = (c) => ({ status: c.method === 'POST' ? 201 : 200, body: c.json ?? {} });
    await manager.sync();
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(['POST /tasks', 'PATCH /tasks/srv']);
    expect(calls[0].json).toMatchObject({ id: created.id, title: 'Offline task' });
    expect(calls[1].json).toEqual({ done: true });
    expect(manager.state).toMatchObject({ pending: 0, conflicts: 0 });
    calls = [];
    await manager.sync();
    expect(calls).toEqual([]);
  });

  it('keeps new writes behind queued ones even when online', async () => {
    const { client, manager } = setup();
    down = true;
    const t = await client.createTask({ title: 'A' });
    await settle(manager);
    down = false;
    reply = (c) => ({ status: c.method === 'POST' ? 201 : 200, body: c.json ?? {} });
    const edited = await client.updateTask(t.id, { title: 'A2' });
    expect(edited).toMatchObject({ id: t.id, title: 'A2' });
    await settle(manager);
    // Folded into the create: one request.
    expect(calls.filter((c) => c.method !== 'GET').map((c) => [c.method, c.json?.title])).toEqual([['POST', 'A2']]);
  });

  it('never sends a row created and deleted offline', async () => {
    const { client, manager } = setup();
    down = true;
    const n = await client.createNote({ body: 'scratch' });
    expect(await client.note(n.id)).toMatchObject({ id: n.id, body: 'scratch' }); // opened before it synced
    await client.updateNote(n.id, { body: 'scratch 2' });
    expect(await client.note(n.id)).toMatchObject({ body: 'scratch 2' });
    await client.deleteNote(n.id);
    expect(manager.state.pending).toBe(0);
    down = false;
    await manager.sync();
    expect(calls).toEqual([]);
  });

  it('queues expenses and incomes', async () => {
    const { client, manager } = setup();
    reply = () => ({ body: { expenses: [], summary: { total_by_currency: {}, by_category: {}, count: 0 } } });
    await client.expenses('2026-10');
    down = true;
    const e = await client.createExpense({ date: '2026-10-21', merchant: 'Taxi', total: 20, currency: 'MAD', category: 'transport', items: [], note: null, receipt_key: null });
    expect(e).toMatchObject({ merchant: 'Taxi', total: 20 });
    const listing = await client.expenses('2026-10');
    expect(listing.expenses.map((x) => x.id)).toEqual([e.id]);
    expect(listing.summary.total_by_currency).toEqual({ MAD: 20 });
    const inc = await client.createIncome({ amount: 500, source: 'Client', date: '2026-10-21' });
    expect(inc).toMatchObject({ amount: 500, source: 'Client', recurring: false });
    await client.deleteIncome('srv-income');
    expect(manager.state.pending).toBe(3);
    await settle(manager);
    down = false;
    reply = (c) => ({ status: c.method === 'DELETE' ? 404 : 201, body: {} });
    const r = await manager.sync();
    expect(r).toMatchObject({ synced: 3, conflicts: 0 }); // a DELETE answered 404 is already done
  });

  it('turns a refused replay into a conflict the user can retry or discard', async () => {
    const { client, manager } = setup();
    down = true;
    await client.updateTask('gone', { title: 'x' });
    down = false;
    reply = () => ({ status: 404, body: { detail: 'task not found' } });
    await manager.sync();
    expect(manager.state).toMatchObject({ pending: 0, conflicts: 1 });
    const [c] = await manager.items();
    expect(c).toMatchObject({ status: 'conflict', httpStatus: 404, error: 'task not found', label: 'Edit task "x"' });

    reply = () => ({ status: 200, body: task('gone', 'x') });
    await manager.retry(c.seq as number);
    await settle(manager);
    expect(manager.state).toMatchObject({ pending: 0, conflicts: 0 });
  });

  it('does not queue requests the hub refused', async () => {
    const { client, manager } = setup();
    reply = () => ({ status: 422, body: { detail: 'title cannot be blank' } });
    await expect(client.createTask({ title: ' ' })).rejects.toMatchObject({ status: 422 });
    expect(manager.state.pending).toBe(0);
  });
});

describe('voice recordings', () => {
  it('keeps a recording made offline and uploads it later with its idempotency key', async () => {
    const { client, manager } = setup();
    down = true;
    const res = await client.voiceNote(new Blob(['OggS-audio'], { type: 'audio/ogg' }), { mode: 'auto', notebook: 'Work', filename: 'memo.ogg' });
    expect(isQueuedVoice(res)).toBe(true);
    const [item] = await manager.items();
    expect(item).toMatchObject({ kind: 'voice', path: '/notes/voice', label: 'Voice note recording' });
    expect(item.upload?.fields).toMatchObject({ mode: 'auto', notebook: 'Work', idempotency_key: item.resourceId });

    down = false;
    reply = () => ({ status: 201, body: { transcript: 'hi', note: null, tasks: [], audio_key: 'voice/x.ogg' } });
    await manager.sync();
    expect(calls).toHaveLength(1);
    const form = calls[0].form as FormData;
    expect(calls[0].path).toBe('/notes/voice');
    expect(form.get('idempotency_key')).toBe(item.resourceId);
    expect(form.get('notebook')).toBe('Work');
    const file = form.get('file') as File;
    expect(await file.text()).toBe('OggS-audio');
    expect(file.name).toBe('memo.ogg');
    expect(manager.state.pending).toBe(0);
  });

  it('uploads directly online, with a key so a retry is not processed twice', async () => {
    const { client } = setup();
    reply = () => ({ status: 201, body: { transcript: 't', audio_key: 'k', expenses: [], incomes: [] } });
    const res = await client.voiceMoney(new Blob(['a']), { kind: 'expense' });
    expect(isQueuedVoice(res)).toBe(false);
    expect(String(calls[0].form?.get('idempotency_key'))).toMatch(/^o[0-9A-Za-z]{15}$/);
  });
});
