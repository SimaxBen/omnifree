import { IDBFactory } from 'fake-indexeddb';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OfflineManager } from '../offline/manager';
import { MAX_SERVER_ERRORS, type QueueItem } from '../offline/model';
import { enqueue, makeSender, replay, type SendResult } from '../offline/queue';
import { CACHE_LIMIT, IdbStore, MemoryStore } from '../offline/store';

let store: IdbStore;
beforeEach(() => {
  store = new IdbStore(new IDBFactory(), `test-${Math.random()}`);
});

function q(p: Partial<QueueItem> & Pick<QueueItem, 'kind' | 'op' | 'resourceId'>): QueueItem {
  return {
    method: p.op === 'create' || p.op === 'upload' ? 'POST' : p.op === 'update' ? 'PATCH' : 'DELETE',
    path: p.op === 'create' ? '/tasks' : `/tasks/${p.resourceId}`,
    createdAt: 1,
    status: 'pending',
    attempts: 0,
    label: `${p.op} ${p.resourceId}`,
    ...p,
  };
}

/** A scripted hub: answers per call, records what was sent. */
function hub(answers: Record<string, number | number[]> = {}) {
  const sent: QueueItem[] = [];
  const send = vi.fn(async (item: QueueItem): Promise<SendResult> => {
    sent.push(item);
    const a = answers[`${item.method} ${item.path}`];
    const status = Array.isArray(a) ? (a.shift() ?? 200) : (a ?? 200);
    return { status, body: status >= 400 ? { detail: `status ${status}` } : { ok: true } };
  });
  return { send, sent };
}

describe('IndexedDB store', () => {
  it('keeps the queue in enqueue order with sequence numbers', async () => {
    const a = await store.queueAdd(q({ kind: 'task', op: 'create', resourceId: 'a' }));
    const b = await store.queueAdd(q({ kind: 'task', op: 'create', resourceId: 'b' }));
    expect(b).toBeGreaterThan(a);
    const all = await store.queueAll();
    expect(all.map((i) => [i.seq, i.resourceId])).toEqual([[a, 'a'], [b, 'b']]);
    await store.queuePut({ ...all[0], status: 'conflict' });
    await store.queueDelete(b);
    expect((await store.queueAll()).map((i) => [i.resourceId, i.status])).toEqual([['a', 'conflict']]);
  });

  it('stores recordings (ArrayBuffer) and cache bodies', async () => {
    const data = new TextEncoder().encode('OggS').buffer;
    await store.queueAdd(q({ kind: 'voice', op: 'upload', resourceId: 'k', upload: { data, type: 'audio/ogg', filename: 'v.ogg', fields: { mode: 'auto' } } }));
    const [it] = await store.queueAll();
    expect(new TextDecoder().decode(it.upload!.data)).toBe('OggS');
    await store.cachePut('/tasks', { tasks: [{ id: 'a' }] }, 42);
    expect(await store.cacheGet('/tasks')).toEqual({ key: '/tasks', body: { tasks: [{ id: 'a' }] }, savedAt: 42 });
    expect(await store.cacheGet('/nope')).toBeUndefined();
  });

  it('prunes the oldest cache entries', async () => {
    for (let i = 0; i < CACHE_LIMIT + 3; i++) await store.cachePut(`/k${i}`, i, i);
    const keys = (await store.cacheAll()).map((e) => e.key);
    expect(keys).toHaveLength(CACHE_LIMIT);
    expect(keys).not.toContain('/k0');
    expect(keys).toContain(`/k${CACHE_LIMIT + 2}`);
  });

  it('falls back to memory without IndexedDB', async () => {
    const mem = new IdbStore(undefined);
    await mem.queueAdd(q({ kind: 'task', op: 'create', resourceId: 'a' }));
    expect(await mem.queueAll()).toHaveLength(1);
  });
});

describe('enqueue and replay', () => {
  it('replays in order and empties the queue', async () => {
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'o1', body: { id: 'o1', title: 'A' } }));
    await enqueue(store, q({ kind: 'note', op: 'create', resourceId: 'n1', path: '/notes', body: { id: 'n1', body: 'x' } }));
    await enqueue(store, q({ kind: 'task', op: 'update', resourceId: 'srv', body: { done: true } }));
    const { send, sent } = hub();
    const r = await replay(store, send);
    expect(r).toEqual({ synced: 3, conflicts: 0, stopped: false, remaining: 0 });
    expect(sent.map((i) => `${i.method} ${i.path}`)).toEqual(['POST /tasks', 'POST /notes', 'PATCH /tasks/srv']);
    expect(await store.queueAll()).toEqual([]);
  });

  it('coalesces a create with its edits and a delete with its create', async () => {
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'o1', body: { id: 'o1', title: 'A' } }));
    await enqueue(store, q({ kind: 'task', op: 'update', resourceId: 'o1', body: { title: 'B' } }));
    await enqueue(store, q({ kind: 'task', op: 'update', resourceId: 'o1', body: { done: true } }));
    let all = await store.queueAll();
    expect(all).toHaveLength(1);
    expect(all[0].body).toEqual({ id: 'o1', title: 'B', done: true });
    await enqueue(store, q({ kind: 'task', op: 'delete', resourceId: 'o1' }));
    all = await store.queueAll();
    expect(all).toEqual([]);
  });

  it('stops on network failure and keeps the rest for later', async () => {
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'a', body: { id: 'a' } }));
    await enqueue(store, q({ kind: 'task', op: 'update', resourceId: 'b', body: { title: 'x' } }));
    const send = vi.fn(async (): Promise<SendResult> => ({ status: 0 }));
    const r = await replay(store, send);
    expect(r).toMatchObject({ synced: 0, stopped: true, remaining: 2 });
    expect(send).toHaveBeenCalledTimes(1);
    const [first] = await store.queueAll();
    expect(first).toMatchObject({ attempts: 1, error: 'Hub unreachable', status: 'pending' });
    // A thrown sender counts as unreachable too.
    expect((await replay(store, async () => Promise.reject(new Error('boom')))).stopped).toBe(true);
  });

  it('turns 404/409 into conflicts and goes on with the next entries', async () => {
    await enqueue(store, q({ kind: 'task', op: 'update', resourceId: 'gone', body: { done: true } }));
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'dup', body: { id: 'dup' }, path: '/tasks' }));
    await enqueue(store, q({ kind: 'task', op: 'delete', resourceId: 'old' }));
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'ok', body: { id: 'ok' }, path: '/money/incomes' }));
    const { send } = hub({ 'PATCH /tasks/gone': 404, 'POST /tasks': 409, 'DELETE /tasks/old': 404 });
    const r = await replay(store, send);
    expect(r).toEqual({ synced: 2, conflicts: 2, stopped: false, remaining: 0 });
    const left = await store.queueAll();
    expect(left.map((i) => [i.resourceId, i.status, i.httpStatus, i.error])).toEqual([
      ['gone', 'conflict', 404, 'status 404'],
      ['dup', 'conflict', 409, 'status 409'],
    ]);
    // Conflicts are not replayed again on their own.
    send.mockClear();
    expect((await replay(store, send)).synced).toBe(0);
    expect(send).not.toHaveBeenCalled();
  });

  it('gives up on a row after repeated server errors', async () => {
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'a', body: { id: 'a' } }));
    const { send } = hub({ 'POST /tasks': 500 });
    for (let i = 1; i < MAX_SERVER_ERRORS; i++) expect((await replay(store, send)).stopped).toBe(true);
    const r = await replay(store, send);
    expect(r).toMatchObject({ conflicts: 1, stopped: false });
  });

  it('stops on an auth error without marking a conflict', async () => {
    await enqueue(store, q({ kind: 'task', op: 'create', resourceId: 'a', body: { id: 'a' } }));
    const r = await replay(store, hub({ 'POST /tasks': 401 }).send);
    expect(r.stopped).toBe(true);
    expect((await store.queueAll())[0].status).toBe('pending');
  });
});

describe('the manager', () => {
  it('syncs, counts, retries and discards', async () => {
    let online = false;
    const m = new OfflineManager(store, () => online, () => 1000);
    const { send, sent } = hub({ 'PATCH /tasks/x': [404, 200] });
    m.setSender(send);
    const refreshed = vi.fn();
    m.onRefresh(refreshed);

    await m.enqueue(q({ kind: 'task', op: 'create', resourceId: 'a', body: { id: 'a' } }));
    await m.enqueue(q({ kind: 'task', op: 'update', resourceId: 'x', body: { done: true } }));
    expect(m.state).toMatchObject({ pending: 2, conflicts: 0 });
    expect(await m.sync()).toBeNull(); // browser offline: nothing sent
    expect(sent).toHaveLength(0);

    online = true;
    const r = await m.sync();
    expect(r).toMatchObject({ synced: 1, conflicts: 1 });
    expect(m.state).toMatchObject({ pending: 0, conflicts: 1, syncing: false, lastSync: 1000 });
    expect(refreshed).toHaveBeenCalled();

    const [conflict] = await m.items();
    await m.retry(conflict.seq as number);
    await m.sync();
    expect(m.state).toMatchObject({ pending: 0, conflicts: 0 });

    await store.queueAdd({ ...conflict, seq: undefined, status: 'conflict' });
    await m.counts();
    expect(m.state.conflicts).toBe(1);
    await m.discard((await m.items())[0].seq as number);
    expect(m.state.conflicts).toBe(0);
  });

  it('shares one run between concurrent sync calls', async () => {
    const m = new OfflineManager(new MemoryStore(), () => true);
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const send = vi.fn(async (): Promise<SendResult> => {
      await gate;
      return { status: 201 };
    });
    m.setSender(send);
    await m.store.queueAdd(q({ kind: 'task', op: 'create', resourceId: 'a', body: { id: 'a' } }));
    const p1 = m.sync();
    const p2 = m.sync();
    release();
    await Promise.all([p1, p2]);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('tracks saved data on screen and live data coming back', async () => {
    const m = new OfflineManager(new MemoryStore(), () => true);
    const refreshed = vi.fn();
    m.onRefresh(refreshed);
    m.servedFromCache(500);
    m.servedFromCache(300);
    m.servedFromCache(900);
    expect(m.state).toMatchObject({ online: false, cachedAt: 300 });
    expect(m.isOnline()).toBe(false);
    m.reached();
    expect(m.state).toMatchObject({ online: true, cachedAt: null });
    expect(m.isOnline()).toBe(true);
    expect(refreshed).toHaveBeenCalledTimes(1);
    m.reached(); // already live: no extra reload
    expect(refreshed).toHaveBeenCalledTimes(1);
  });

  it('replays when the browser comes back online', async () => {
    let online = false;
    const m = new OfflineManager(new MemoryStore(), () => online);
    const { send } = hub();
    m.setSender(send);
    const listeners: Record<string, () => void> = {};
    const win = { addEventListener: (t: string, f: () => void) => (listeners[t] = f), removeEventListener: vi.fn() };
    const stop = m.start(win as unknown as Window);
    await m.enqueue(q({ kind: 'task', op: 'create', resourceId: 'a', body: { id: 'a' } }));
    expect(send).not.toHaveBeenCalled();
    online = true;
    listeners.online();
    await vi.waitFor(() => expect(m.state.pending).toBe(0));
    expect(send).toHaveBeenCalledTimes(1);
    stop();
    expect(win.removeEventListener).toHaveBeenCalledTimes(2);
  });
});

describe('the fetch sender', () => {
  it('sends JSON and multipart bodies with the bearer token', async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      return new Response(JSON.stringify({ id: 'o1' }), { status: 201, headers: { 'content-type': 'application/json' } });
    });
    const send = makeSender('https://hub.example/', 'tok', fetchMock as unknown as typeof fetch);
    const r = await send(q({ kind: 'task', op: 'create', resourceId: 'o1', body: { id: 'o1', title: 'A' } }));
    expect(r).toEqual({ status: 201, body: { id: 'o1' } });
    expect(calls[0].url).toBe('https://hub.example/tasks');
    expect(calls[0].init.headers).toMatchObject({ Authorization: 'Bearer tok', 'Content-Type': 'application/json' });
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ id: 'o1', title: 'A' });

    const data = new TextEncoder().encode('OggS').buffer;
    await send(q({ kind: 'voice', op: 'upload', resourceId: 'key-1', path: '/notes/voice', upload: { data, type: 'audio/ogg', filename: 'v.ogg', fields: { mode: 'auto', idempotency_key: 'key-1' } } }));
    const form = calls[1].init.body as FormData;
    expect(form.get('mode')).toBe('auto');
    expect(form.get('idempotency_key')).toBe('key-1');
    expect(await (form.get('file') as Blob).text()).toBe('OggS');

    await send(q({ kind: 'task', op: 'delete', resourceId: 'x' }));
    expect(calls[2].init.body).toBeUndefined();

    const down = makeSender('https://hub.example', '', (async () => Promise.reject(new TypeError('offline'))) as unknown as typeof fetch);
    expect(await down(q({ kind: 'task', op: 'delete', resourceId: 'x' }))).toEqual({ status: 0 });
  });
});
