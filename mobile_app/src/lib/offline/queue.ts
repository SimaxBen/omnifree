// Offline mode, the write queue: enqueue with coalescing, and the in-order replay against the hub.

import { MAX_SERVER_ERRORS, classify, detailOf, planEnqueue, type QueueItem } from './model';
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
}

/** Queue ``next``, folding it into earlier entries of the same row when possible. */
export async function enqueue(store: OfflineStore, next: QueueItem): Promise<void> {
  const plan = planEnqueue(await store.queueAll(), next);
  for (const seq of plan.drop) await store.queueDelete(seq);
  if (plan.update) await store.queuePut(plan.update);
  if (plan.add) await store.queueAdd(plan.add);
}

/** Replays the pending entries in enqueue order; conflicts are kept aside for the user to retry or discard. */
export async function replay(store: OfflineStore, send: Send): Promise<ReplayResult> {
  const items = (await store.queueAll()).filter((i) => i.status === 'pending');
  let synced = 0;
  let conflicts = 0;
  let stopped = false;
  for (const item of items) {
    let res: SendResult;
    try {
      res = await send(item);
    } catch {
      res = { status: 0 };
    }
    let outcome = classify(item.method, res.status);
    const attempts = item.attempts + 1;
    if (outcome === 'retry' && res.status >= 500 && attempts >= MAX_SERVER_ERRORS) outcome = 'conflict';
    if (outcome === 'done') {
      await store.queueDelete(item.seq as number);
      synced++;
      continue;
    }
    const error = detailOf(res.body, res.status);
    if (outcome === 'conflict') {
      await store.queuePut({ ...item, status: 'conflict', attempts, httpStatus: res.status, error });
      conflicts++;
      continue;
    }
    await store.queuePut({ ...item, attempts, httpStatus: res.status, error });
    stopped = true;
    break;
  }
  const remaining = (await store.queueAll()).filter((i) => i.status === 'pending').length;
  return { synced, conflicts, stopped, remaining };
}

/** Puts a conflict back in line, at its original place in the enqueue order. */
export async function retryItem(store: OfflineStore, seq: number): Promise<void> {
  const item = (await store.queueAll()).find((i) => i.seq === seq);
  if (!item) return;
  await store.queuePut({ ...item, status: 'pending', attempts: 0, error: undefined, httpStatus: undefined });
}

export async function discardItem(store: OfflineStore, seq: number): Promise<void> {
  await store.queueDelete(seq);
}

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
    let res: Response;
    try {
      res = await fetchImpl(base + item.path, { method: item.method, headers, body });
    } catch {
      return { status: 0 };
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
