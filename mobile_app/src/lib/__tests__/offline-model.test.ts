import { describe, expect, it } from 'vitest';
import {
  PENDING,
  bannerText,
  cacheKey,
  classify,
  isCacheableGet,
  isPending,
  labelFor,
  matchMutation,
  newClientId,
  optimisticCreate,
  optimisticUpdate,
  overlay,
  pendingCreate,
  planEnqueue,
  type QueueItem,
} from '../offline/model';

const T0 = Date.UTC(2026, 9, 22, 9, 30);

function item(p: Partial<QueueItem> & Pick<QueueItem, 'kind' | 'op' | 'resourceId'>): QueueItem {
  return {
    method: p.op === 'create' || p.op === 'upload' ? 'POST' : p.op === 'update' ? 'PATCH' : 'DELETE',
    path: '/x',
    createdAt: T0,
    status: 'pending',
    attempts: 0,
    label: 'x',
    ...p,
  };
}

describe('request matching', () => {
  it('knows which GETs are cached', () => {
    for (const p of ['/tasks', '/notes', '/notes/abc', '/expenses', '/money/overview', '/money/incomes', '/gcal/events', '/chats', '/chats/c1']) {
      expect(isCacheableGet(p)).toBe(true);
    }
    for (const p of ['/notes/voice', '/drive/files', '/digest', '/links', '/tasks/abc/x', '/money/fx', '/budget']) {
      expect(isCacheableGet(p)).toBe(false);
    }
  });

  it('builds stable cache keys', () => {
    expect(cacheKey('/tasks')).toBe('/tasks');
    expect(cacheKey('/tasks', { status: 'open', due_before: undefined })).toBe('/tasks?status=open');
    expect(cacheKey('/notes', { q: '', limit: 100, notebook: 'Work' })).toBe(cacheKey('/notes', { notebook: 'Work', limit: 100 }));
    expect(cacheKey('/notes', { notebook: 'A&B' })).toBe('/notes?notebook=A%26B');
  });

  it('classifies the queued writes', () => {
    expect(matchMutation('POST', '/tasks')).toEqual({ kind: 'task', op: 'create', method: 'POST' });
    expect(matchMutation('patch', '/tasks/t1')).toEqual({ kind: 'task', op: 'update', method: 'PATCH', resourceId: 't1' });
    expect(matchMutation('DELETE', '/notes/n%201')).toEqual({ kind: 'note', op: 'delete', method: 'DELETE', resourceId: 'n 1' });
    expect(matchMutation('POST', '/expenses')?.kind).toBe('expense');
    expect(matchMutation('PATCH', '/money/incomes/i1')).toMatchObject({ kind: 'income', op: 'update', resourceId: 'i1' });
    expect(matchMutation('POST', '/notes/voice')).toEqual({ kind: 'voice', op: 'upload', method: 'POST' });
    expect(matchMutation('POST', '/money/voice')?.kind).toBe('voice');
    for (const [m, p] of [['POST', '/expenses/receipt'], ['PATCH', '/money/recurring/r1'], ['PUT', '/chats/c1'], ['POST', '/links'], ['GET', '/tasks'], ['PATCH', '/tasks/a/b']]) {
      expect(matchMutation(m, p)).toBeNull();
    }
  });

  it('makes client ids the hub accepts', () => {
    const ids = new Set(Array.from({ length: 200 }, newClientId));
    expect(ids.size).toBe(200);
    for (const id of ids) expect(id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  });
});

describe('optimistic objects', () => {
  it('creates rows with the hub defaults', () => {
    const t = optimisticCreate('task', 'o1', { id: 'o1', title: ' Buy milk ', priority: 'high' }, T0);
    expect(t).toMatchObject({ id: 'o1', title: 'Buy milk', done: false, priority: 'high', due: null, created_at: '2026-10-22T09:30:00Z' });
    expect(isPending(t)).toBe(true);
    const n = optimisticCreate('note', 'o2', { body: '## **Groceries** list\n- milk' }, T0);
    expect(n).toMatchObject({ title: 'Groceries list', notebook: 'Inbox', tags: [], pinned: false });
    const e = optimisticCreate('expense', 'o3', { total: '12.5', merchant: 'Café' }, T0);
    expect(e).toMatchObject({ total: 12.5, currency: 'MAD', category: 'other', date: expect.stringMatching(/^2026-10-2\d$/) });
    expect(optimisticCreate('income', 'o4', { amount: 900, date: '2026-10-01' }, T0)).toMatchObject({ amount: 900, date: '2026-10-01', recurring: false });
  });

  it('updates tasks like the hub (done_at, due_time)', () => {
    const base = { id: 't', title: 'x', done: false, done_at: null, due: '2026-10-23', due_time: '09:00' };
    const done = optimisticUpdate('task', base, { done: true }, T0);
    expect(done.done_at).toBe(new Date(T0).toISOString());
    expect(optimisticUpdate('task', { ...done, [PENDING]: undefined }, { done: false }, T0).done_at).toBeNull();
    expect(optimisticUpdate('task', base, { due: null }, T0).due_time).toBeNull();
  });
});

describe('overlay of pending writes on GET bodies', () => {
  const tasks = { tasks: [{ id: 'a', title: 'A', done: false }, { id: 'b', title: 'B', done: false }] };

  it('adds creates, merges updates, removes deletes', () => {
    const q = [
      item({ kind: 'task', op: 'create', resourceId: 'o1', body: { title: 'New' } }),
      item({ kind: 'task', op: 'update', resourceId: 'a', body: { title: 'A2' } }),
      item({ kind: 'task', op: 'delete', resourceId: 'b' }),
      item({ kind: 'note', op: 'create', resourceId: 'n1', body: { body: 'x' } }),
    ];
    const out = overlay('/tasks', { status: 'open' }, tasks, q) as typeof tasks;
    expect(out.tasks.map((t) => [t.id, t.title])).toEqual([['a', 'A2'], ['o1', 'New']]);
    expect(out.tasks.every(isPending)).toBe(true);
    expect(tasks.tasks).toHaveLength(2); // input untouched
  });

  it('respects the task status filter', () => {
    const q = [item({ kind: 'task', op: 'update', resourceId: 'a', body: { done: true } })];
    expect((overlay('/tasks', { status: 'open' }, tasks, q) as typeof tasks).tasks.map((t) => t.id)).toEqual(['b']);
    expect((overlay('/tasks', { status: 'all' }, tasks, q) as typeof tasks).tasks).toHaveLength(2);
    const created = [item({ kind: 'task', op: 'create', resourceId: 'o1', body: { title: 'N' } })];
    expect((overlay('/tasks', { status: 'done' }, { tasks: [] }, created) as typeof tasks).tasks).toEqual([]);
  });

  it('filters notes by notebook and skips searches', () => {
    const q = [item({ kind: 'note', op: 'create', resourceId: 'n1', body: { body: 'hi', notebook: 'Work' } })];
    const body = { notes: [{ id: 'old' }], notebooks: [] };
    expect((overlay('/notes', {}, body, q) as typeof body).notes.map((n) => n.id)).toEqual(['n1', 'old']);
    expect((overlay('/notes', { notebook: 'Work' }, body, q) as typeof body).notes).toHaveLength(2);
    expect(overlay('/notes', { notebook: 'Home' }, body, q)).toBe(body);
    expect(overlay('/notes', { q: 'hi' }, body, q)).toBe(body);
  });

  it('keeps expenses to the month and recomputes the summary', () => {
    const body = { expenses: [{ id: 'e1', date: '2026-10-02', total: 10, currency: 'MAD', category: 'food' }], summary: { total_by_currency: { MAD: 10 }, by_category: {}, count: 1 } };
    const q = [
      item({ kind: 'expense', op: 'create', resourceId: 'o1', body: { date: '2026-10-05', total: 5.5, category: 'food' } }),
      item({ kind: 'expense', op: 'create', resourceId: 'o2', body: { date: '2026-09-30', total: 99 } }),
    ];
    const out = overlay('/expenses', { month: '2026-10' }, body, q) as { expenses: { id: string }[]; summary: { total_by_currency: Record<string, number>; count: number } };
    expect(out.expenses.map((e) => e.id)).toEqual(['e1', 'o1']);
    expect(out.summary).toMatchObject({ total_by_currency: { MAD: 15.5 }, count: 2 });
  });

  it('merges note edits into an opened note and leaves unknown bodies alone', () => {
    const q = [item({ kind: 'note', op: 'update', resourceId: 'n1', body: { title: 'T2' } }), item({ kind: 'note', op: 'update', resourceId: 'n2', body: { title: 'other' } })];
    expect(overlay('/notes/n1', undefined, { id: 'n1', title: 'T' }, q)).toMatchObject({ id: 'n1', title: 'T2', [PENDING]: true });
    const ov = { month: '2026-10' };
    expect(overlay('/money/overview', undefined, ov, q)).toBe(ov);
    expect(overlay('/tasks', undefined, tasks, [{ ...q[0], status: 'conflict' }])).toBe(tasks);
  });

  it('answers a note created offline', () => {
    const q = [
      item({ kind: 'note', op: 'create', resourceId: 'n1', body: { body: 'one' } }),
      item({ kind: 'note', op: 'update', resourceId: 'n1', body: { body: 'two' } }),
    ];
    expect(pendingCreate(q, 'note', 'n1')).toMatchObject({ id: 'n1', body: 'two' });
    expect(pendingCreate(q, 'note', 'other')).toBeNull();
    expect(pendingCreate([...q, item({ kind: 'note', op: 'delete', resourceId: 'n1' })], 'note', 'n1')).toBeNull();
  });
});

describe('queue coalescing', () => {
  const create = { ...item({ kind: 'task', op: 'create', resourceId: 'o1', body: { id: 'o1', title: 'A' } }), seq: 1 };

  it('folds updates into the queued create, only with the fields POST accepts', () => {
    const plan = planEnqueue([create], item({ kind: 'task', op: 'update', resourceId: 'o1', body: { title: 'B', priority: 'high' } }));
    expect(plan.add).toBeUndefined();
    expect(plan.update).toMatchObject({ seq: 1, op: 'create', body: { id: 'o1', title: 'B', priority: 'high' } });
    // `done` is not a TaskCreate field: it follows the create as its own PATCH.
    const tick = planEnqueue([create], item({ kind: 'task', op: 'update', resourceId: 'o1', body: { title: 'C', done: true } }));
    expect(tick.update?.body).toEqual({ id: 'o1', title: 'C' });
    expect(tick.add).toMatchObject({ op: 'update', method: 'PATCH', body: { done: true } });
    // Clearing the day of a queued create clears its time too (POST refuses a time without a day).
    const timed = { ...create, body: { id: 'o1', title: 'A', due: '2026-10-22', due_time: '09:00' } };
    expect(planEnqueue([timed], item({ kind: 'task', op: 'update', resourceId: 'o1', body: { due: null } })).update?.body).toEqual({ id: 'o1', title: 'A', due: null, due_time: null });
  });

  it('never folds into an entry being sent', () => {
    const plan = planEnqueue([{ ...create, inFlight: true }], item({ kind: 'task', op: 'update', resourceId: 'o1', body: { title: 'B' } }));
    expect(plan.update).toBeUndefined();
    expect(plan.add).toMatchObject({ op: 'update', body: { title: 'B' } });
  });

  it('lets a create the hub refused absorb its edits and its delete', () => {
    const refused = { ...create, status: 'conflict' as const };
    expect(planEnqueue([refused], item({ kind: 'task', op: 'update', resourceId: 'o1', body: { title: 'B' } })).update).toMatchObject({ seq: 1, status: 'conflict', body: { title: 'B' } });
    expect(planEnqueue([refused], item({ kind: 'task', op: 'delete', resourceId: 'o1' }))).toEqual({ drop: [1] });
  });

  it('shows a task ticked before its create synced as done', () => {
    const tick = { ...item({ kind: 'task', op: 'update', resourceId: 'o1', body: { done: true } }), seq: 2 };
    const out = overlay('/tasks', { status: 'done' }, { tasks: [] }, [create, tick]) as { tasks: Record<string, unknown>[] };
    expect(out.tasks.map((t) => [t.id, t.done])).toEqual([['o1', true]]);
    const open = overlay('/tasks', { status: 'open' }, { tasks: [] }, [create, tick]) as { tasks: unknown[] };
    expect(open.tasks).toEqual([]);
  });

  it('folds consecutive updates of one row', () => {
    const upd = { ...item({ kind: 'note', op: 'update', resourceId: 'n', body: { body: 'a' } }), seq: 4 };
    const plan = planEnqueue([upd], item({ kind: 'note', op: 'update', resourceId: 'n', body: { body: 'ab', title: 't' } }));
    expect(plan.update).toMatchObject({ seq: 4, body: { body: 'ab', title: 't' } });
    // Another row, or a conflict entry, is never touched.
    expect(planEnqueue([upd], item({ kind: 'note', op: 'update', resourceId: 'm', body: {} })).add).toBeDefined();
    expect(planEnqueue([{ ...upd, status: 'conflict' }], item({ kind: 'note', op: 'update', resourceId: 'n', body: {} })).add).toBeDefined();
  });

  it('drops a create deleted before it was sent', () => {
    const upd = { ...item({ kind: 'task', op: 'update', resourceId: 'o1', body: {} }), seq: 2 };
    const plan = planEnqueue([create, upd], item({ kind: 'task', op: 'delete', resourceId: 'o1' }));
    expect(plan).toEqual({ drop: [1, 2] });
  });

  it('queues a delete after dropping the row updates', () => {
    const upd = { ...item({ kind: 'task', op: 'update', resourceId: 'srv', body: {} }), seq: 7 };
    const plan = planEnqueue([upd], item({ kind: 'task', op: 'delete', resourceId: 'srv' }));
    expect(plan.drop).toEqual([7]);
    expect(plan.add?.op).toBe('delete');
  });
});

describe('replay outcomes and labels', () => {
  it('classifies statuses', () => {
    expect(classify('POST', 201)).toBe('done');
    expect(classify('POST', 200)).toBe('done');
    expect(classify('DELETE', 404)).toBe('done');
    expect(classify('PATCH', 404)).toBe('conflict');
    expect(classify('POST', 409)).toBe('conflict');
    expect(classify('POST', 422)).toBe('conflict');
    expect(classify('POST', 0)).toBe('retry');
    expect(classify('POST', 503)).toBe('retry');
    expect(classify('POST', 429)).toBe('retry');
    expect(classify('POST', 401)).toBe('stop');
  });

  it('labels queue entries', () => {
    expect(labelFor('task', 'create', { title: 'Buy milk' })).toBe('New task "Buy milk"');
    expect(labelFor('task', 'update', { done: true }, { title: 'Buy milk' })).toBe('Complete task "Buy milk"');
    expect(labelFor('expense', 'create', { merchant: 'Café', total: 12, currency: 'MAD' })).toBe('New expense "Café 12 MAD"');
    expect(labelFor('note', 'delete', undefined, { title: 'Ideas' })).toBe('Delete note "Ideas"');
    expect(labelFor('voice', 'upload')).toBe('Upload voice recording');
  });

  it('writes the banner line', () => {
    const base = { online: true, cachedAt: null, pending: 0, conflicts: 0, syncing: false };
    expect(bannerText(base)).toBeNull();
    const at = new Date(2026, 9, 22, 8, 5).getTime();
    expect(bannerText({ ...base, online: false, cachedAt: at })).toEqual({ text: 'Offline — showing saved data from 08:05', tone: 'offline' });
    expect(bannerText({ ...base, online: false, pending: 2 })?.text).toBe('Offline — changes sync when you are back online · 2 pending sync');
    expect(bannerText({ ...base, pending: 1, syncing: true })).toEqual({ text: 'syncing 1…', tone: 'sync' });
    expect(bannerText({ ...base, conflicts: 1 })).toEqual({ text: '1 needs attention', tone: 'conflict' });
  });
});
