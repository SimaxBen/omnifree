// Offline mode, the write queue: enqueue with coalescing, and the in-order replay against the hub.

import { MAX_SERVER_ERRORS, classify, detailOf, planEnqueue, retryDelayMs, type QueueItem } from './model';
import type { OfflineStore } from './store';

export interface SendResult {
  status: number;
  body?: unknown;
}

/** Sends one queued write to the hub; network failures answer status 0 (never throw). */
export type Send = (item: QueueItem) => Promise<SendResult>;

export interface ReplayResult {
  synced: number;
  conflicts: number;
  /** True when the replay stopped early (hub unreachable, auth error): the rest waits for the next run. */
  stopped: boolean;
  remaining: number;
  /** The hub could not be reached (status 0): the app is offline. */
  unreachable: boolean;
  /** The hub refused the token (401/403): the network is fine, the settings are not. */
  authError: boolean;
}

export interface ReplayOptions {
  now?: number;
  /** The current hub URL: entries queued for another hub are left alone. */
  hub?: string;
  /** Also retry entries still waiting out a server-error backoff ("Sync now"). */
  force?: boolean;
}

// Queue writes are read-modify-write sequences on IndexedDB: one at a time per store, so an enqueue
// never interleaves with a replay marking or settling an entry.
const locks = new WeakMap<OfflineStore, Promise<unknown>>();

function locked<T>(store: OfflineStore, fn: () => Promise<T>): Promise<T> {
  const run = (locks.get(store) ?? Promise.resolve()).then(fn);
  locks.set(store, run.catch(() => undefined));
  return run;
}

async function bySeq(store: OfflineStore, seq: number | undefined): Promise<QueueItem | undefined> {
  return (await store.queueAll()).find((i) => i.seq === seq);
}

const rowOf = (i: QueueItem) => `${i.kind}:${i.resourceId}`;

/** Queue ``next``, folding it into earlier entries of the same row when possible. */
export function enqueue(store: OfflineStore, next: QueueItem): Promise<void> {
  return locked(store, async () => {
    const plan = planEnqueue(await store.queueAll(), next);
    for (const seq of plan.drop) await store.queueDelete(seq);
    if (plan.update) await store.queuePut(plan.update);
    if (plan.add) await store.queueAdd(plan.add);
  });
}

/**
 * Replays the pending entries in enqueue order; conflicts are kept aside for the user to retry or
 * discard. Entries of one row stay in order: a row whose create is in conflict, or whose entry is
 * waiting out a server error, holds its later entries back while the other rows go on. An entry is
 * marked inFlight while it is sent, so a write made meanwhile is queued apart rather than folded
 * into a body already on the wire.
 */
export async function replay(store: OfflineStore, send: Send, opts: ReplayOptions = {}): Promise<ReplayResult> {
  const now = opts.now ?? Date.now();
  const snapshot = await locked(store, async () => {
    const all = await store.queueAll();
    for (const i of all.filter((x) => x.inFlight)) await store.queuePut({ ...i, inFlight: false }); // left by a run that died
    return all;
  });
  const blocked = new Set(snapshot.filter((i) => i.status === 'conflict' && i.op === 'create').map(rowOf));
  let synced = 0;
  let conflicts = 0;
  let unreachable = false;
  let authError = false;
  const mine = (i: QueueItem) => !i.hub || opts.hub === undefined || i.hub === opts.hub;
  for (const { seq } of snapshot.filter((i) => i.status === 'pending' && mine(i))) {
    // Re-read and mark under the lock: the entry may have absorbed an edit, or been discarded.
    const item = await locked(store, async () => {
      const cur = await bySeq(store, seq);
      if (!cur || cur.status !== 'pending') return null;
      if (blocked.has(rowOf(cur)) || (!opts.force && (cur.nextAttemptAt ?? 0) > now)) {
        blocked.add(rowOf(cur));
        return null;
      }
      const marked = { ...cur, inFlight: true };
      await store.queuePut(marked);
      return marked;
    });
    if (!item) continue;
    let res: SendResult;
    try {
      res = await send(item);
    } catch {
      res = { status: 0 };
    }
    let outcome = classify(item.method, res.status);
    const attempts = item.attempts + 1;
    if (outcome === 'retry' && res.status >= 500 && attempts >= MAX_SERVER_ERRORS) outcome = 'conflict';
    const error = detailOf(res.body, res.status);
    await locked(store, async () => {
      const cur = await bySeq(store, seq); // gone: discarded while it was being sent
      if (outcome === 'done') {
        if (cur) await store.queueDelete(seq as number);
        return;
      }
      if (!cur) return;
      const base: QueueItem = { ...cur, inFlight: false, attempts, httpStatus: res.status, error };
      if (outcome === 'conflict') await store.queuePut({ ...base, status: 'conflict', nextAttemptAt: undefined });
      else if (outcome === 'retry' && res.status !== 0) await store.queuePut({ ...base, nextAttemptAt: now + retryDelayMs(attempts) });
      else await store.queuePut({ ...base, nextAttemptAt: undefined }); // offline or auth: retried as soon as that clears
    });
    if (outcome === 'done') {
      synced++;
      continue;
    }
    if (outcome === 'conflict') {
      conflicts++;
      if (item.op === 'create') blocked.add(rowOf(item));
      continue;
    }
    if (outcome === 'retry' && res.status !== 0) {
      blocked.add(rowOf(item)); // a server error: back off on this row, go on with the others
      continue;
    }
    if (outcome === 'stop') authError = true;
    else unreachable = true;
    break;
  }
  const remaining = (await store.queueAll()).filter((i) => i.status === 'pending' && mine(i)).length;
  return { synced, conflicts, stopped: unreachable || authError, remaining, unreachable, authError };
}

/** Puts a conflict back in line, at its original place in the enqueue order. */
export function retryItem(store: OfflineStore, seq: number): Promise<void> {
  return locked(store, async () => {
    const item = await bySeq(store, seq);
    if (!item) return;
    await store.queuePut({ ...item, status: 'pending', attempts: 0, error: undefined, httpStatus: undefined, nextAttemptAt: undefined });
  });
}

/** Drops an entry; dropping a create also drops the later entries of its row (they would only 404). */
export function discardItem(store: OfflineStore, seq: number): Promise<void> {
  return locked(store, async () => {
    const all = await store.queueAll();
    const item = all.find((i) => i.seq === seq);
    if (!item) return;
    await store.queueDelete(seq);
    if (item.op !== 'create') return;
    for (const other of all) {
      if (other.seq !== seq && rowOf(other) === rowOf(item) && (other.seq ?? 0) > seq) await store.queueDelete(other.seq as number);
    }
  });
}

/** A replayed request is given up (and retried later) after this long; uploads get past Vercel's 60 s limit. */
export const SEND_TIMEOUT_MS = 30_000;
export const UPLOAD_TIMEOUT_MS = 65_000;

/** The fetch-based sender used by the app: same bearer token as HubClient, JSON or multipart bodies. */
export function makeSender(baseUrl: string, token: string, fetchImpl: typeof fetch = (...a) => fetch(...a)): Send {
  const base = baseUrl.replace(/\/+$/, '');
  return async (item) => {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    let body: BodyInit | undefined;
    if (item.upload) {
      const form = new FormData();
      form.append('file', new Blob([item.upload.data], { type: item.upload.type }), item.upload.filename);
      for (const [k, v] of Object.entries(item.upload.fields)) form.append(k, v);
      body = form;
    } else if (item.body !== undefined && item.method !== 'DELETE') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(item.body);
    }
    // Without a deadline a hung request would hold the whole replay (and every later sync) forever.
    const ctrl = typeof AbortController === 'undefined' ? undefined : new AbortController();
    const timer = ctrl ? setTimeout(() => ctrl.abort(), item.upload ? UPLOAD_TIMEOUT_MS : SEND_TIMEOUT_MS) : undefined;
    let res: Response;
    try {
      res = await fetchImpl(base + item.path, { method: item.method, headers, body, signal: ctrl?.signal });
    } catch {
      return { status: 0 };
    } finally {
      clearTimeout(timer);
    }
    const text = await res.text().catch(() => '');
    let parsed: unknown = text;
    try {
      parsed = text ? JSON.parse(text) : undefined;
    } catch {
      parsed = text;
    }
    return { status: res.status, body: parsed };
  };
}
