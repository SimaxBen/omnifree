// Offline mode, the HubClient the app uses: cached GETs for the main lists (served when the hub is out
// of reach), and queued writes (tasks, notes, expenses, incomes, voice recordings) answered
// optimistically and replayed by the OfflineManager. Every typed method of HubClient goes through
// `request`, so overriding it covers them all without touching the views.

import { HubClient, HubError } from '../api';
import type { HubSettings } from '../settings';
import { offline as defaultManager, type OfflineManager } from './manager';
import {
  cacheKey,
  isCacheableGet,
  labelFor,
  matchMutation,
  newClientId,
  optimisticCreate,
  optimisticUpdate,
  overlay,
  pendingCreate,
  type Kind,
  type Mutation,
  type QueueItem,
} from './model';
import { makeSender } from './queue';

type Options = NonNullable<Parameters<HubClient['request']>[2]>;
type Obj = Record<string, unknown>;

/** A cached screen is shown when the hub has not answered within this delay. */
export const GET_TIMEOUT_MS = 12_000;
/** A write sent live is queued instead when the hub has not answered within this delay (replays are idempotent). */
export const WRITE_TIMEOUT_MS = 12_000;
/** Same for a voice upload: past Vercel's 60 s limit, so the first attempt has surely ended before a replay. */
export const VOICE_TIMEOUT_MS = 65_000;

const LIST_FIELDS: Record<string, Kind> = { tasks: 'task', notes: 'note', expenses: 'expense', incomes: 'income' };

/** Result of a voice upload queued offline (VoiceNoteResult-shaped, with `queued: true`). */
export interface QueuedVoiceResult {
  transcript: string;
  note: null;
  tasks: [];
  audio_key: string;
  expenses: [];
  incomes: [];
  queued: true;
}

export function isQueuedVoice(res: unknown): res is QueuedVoiceResult {
  return !!res && typeof res === 'object' && (res as { queued?: unknown }).queued === true;
}

function isNetworkError(err: unknown): boolean {
  return err instanceof HubError && err.status === 0;
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  if (ms <= 0) return p;
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new HubError('The hub did not answer in time', 0)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}

export class OfflineHubClient extends HubClient {
  /** Rows seen in responses, by `${kind}:${id}`: the base of optimistic updates. */
  private known = new Map<string, Obj>();

  constructor(
    settings: HubSettings,
    readonly offline: OfflineManager = defaultManager,
    readonly getTimeoutMs = GET_TIMEOUT_MS,
  ) {
    super(settings);
    offline.setSender(makeSender(this.baseUrl, this.token));
    void offline.bindHub(this.baseUrl, !!this.token);
  }

  override async request<T>(method: string, path: string, opts: Options = {}): Promise<T> {
    const m = method.toUpperCase();
    if (m === 'GET' && isCacheableGet(path)) return (await this.cachedGet(path, opts)) as T;
    const mutation = matchMutation(m, path);
    if (mutation) return (await this.mutate(mutation, path, opts)) as T;
    return super.request<T>(method, path, opts);
  }

  // ------------------------------------------------------------------ reads

  private remember(body: unknown, kind?: Kind): void {
    if (!body || typeof body !== 'object') return;
    const obj = body as Obj;
    if (kind && typeof obj.id === 'string') {
      this.known.set(`${kind}:${obj.id}`, obj);
      return;
    }
    for (const [field, k] of Object.entries(LIST_FIELDS)) {
      const list = obj[field];
      if (Array.isArray(list)) for (const row of list) if (row && typeof row.id === 'string') this.known.set(`${k}:${row.id}`, row as Obj);
    }
  }

  private async cachedGet(path: string, opts: Options): Promise<unknown> {
    const key = cacheKey(path, opts.query);
    const store = this.offline.store;
    const single = /^\/notes\/([^/]+)$/.exec(path);
    if (single) {
      const local = pendingCreate(await this.offline.items(), 'note', decodeURIComponent(single[1]));
      if (local) return local; // created offline: the hub does not know it yet
    }
    const saved = async () => {
      const hit = await store.cacheGet(key).catch(() => undefined);
      return hit && (!hit.hub || hit.hub === this.baseUrl) ? hit : undefined; // never another hub's data
    };
    const fromCache = async (err: unknown) => {
      const hit = await saved();
      if (!hit) throw err;
      this.offline.servedFromCache(hit.savedAt);
      this.remember(hit.body);
      return overlay(path, opts.query, hit.body, await this.offline.pendingItems());
    };
    const browserOffline = typeof navigator !== 'undefined' && navigator.onLine === false;
    if (!this.offline.isOnline() && browserOffline) {
      return fromCache(new HubError('Offline, and this screen was never loaded online', 0));
    }
    if (!this.offline.isOnline()) {
      // The hub is known to be out of reach (captive portal, weak signal): show the saved copy at once
      // and revalidate in the background; a late live answer brings the app back online.
      if (await saved()) {
        const shown = await fromCache(new HubError('Offline', 0)); // marks "saved data on screen" first
        super.request<unknown>('GET', path, opts).then(
          (body) => {
            void store.cachePut(key, body, this.offline.now(), this.baseUrl).catch(() => undefined);
            this.offline.reached();
          },
          () => undefined,
        );
        return shown;
      }
    }
    const live = super.request<unknown>('GET', path, opts);
    // Even when the timeout serves the saved copy first, a late answer still refreshes the cache.
    live.then((body) => store.cachePut(key, body, this.offline.now(), this.baseUrl).catch(() => undefined), () => undefined);
    let body: unknown;
    try {
      body = await withTimeout(live, this.getTimeoutMs);
    } catch (err) {
      if (!isNetworkError(err)) throw err;
      this.offline.unreachable();
      return fromCache(err);
    }
    this.remember(body);
    this.offline.reached();
    return overlay(path, opts.query, body, await this.offline.pendingItems());
  }

  // ------------------------------------------------------------------ writes

  private async base(kind: Kind, id: string, pending: QueueItem[]): Promise<Obj | null> {
    const local = pendingCreate(pending, kind, id);
    if (local) return local;
    const mem = this.known.get(`${kind}:${id}`);
    if (mem) return mem;
    const field = Object.entries(LIST_FIELDS).find(([, k]) => k === kind)?.[0] as string;
    for (const entry of await this.offline.store.cacheAll().catch(() => [])) {
      if (entry.hub && entry.hub !== this.baseUrl) continue;
      const list = entry.body && typeof entry.body === 'object' ? (entry.body as Obj)[field] : undefined;
      const row = Array.isArray(list) ? (list as Obj[]).find((r) => r?.id === id) : undefined;
      if (row) return row;
    }
    return null;
  }

  private async mutate(mutation: Mutation, path: string, opts: Options): Promise<unknown> {
    if (mutation.kind === 'voice') return this.voice(path, opts);
    const kind = mutation.kind;
    const all = await this.offline.items();
    const pending = all.filter((i) => i.status === 'pending');
    let body = opts.json && typeof opts.json === 'object' ? { ...(opts.json as Obj) } : undefined;
    let resourceId = mutation.resourceId ?? '';
    if (mutation.op === 'create') {
      resourceId = typeof body?.id === 'string' && body.id ? body.id : newClientId();
      body = { ...(body ?? {}), id: resourceId }; // the hub dedupes replays on this id
    }
    // A row with queue entries (pending, or a create the hub refused) only changes through the queue.
    const touchesQueued = all.some((i) => i.kind === kind && i.resourceId === resourceId);
    // Online with nothing waiting: send now. Otherwise (or when the network fails or is too slow) queue behind the rest.
    if (this.offline.isOnline() && !pending.length && !touchesQueued) {
      try {
        const res = await withTimeout(super.request<unknown>(mutation.method, path, { ...opts, json: body }), WRITE_TIMEOUT_MS);
        if (mutation.op !== 'delete') this.remember(res, kind);
        else this.known.delete(`${kind}:${resourceId}`);
        return res;
      } catch (err) {
        if (!isNetworkError(err)) throw err;
        this.offline.unreachable();
      }
    }
    const at = this.offline.now();
    const base = mutation.op === 'create' ? null : await this.base(kind, resourceId, all);
    const item: QueueItem = {
      kind,
      op: mutation.op,
      method: mutation.method,
      path,
      resourceId,
      body: mutation.op === 'delete' ? undefined : body,
      createdAt: at,
      status: 'pending',
      attempts: 0,
      label: labelFor(kind, mutation.op, body, base),
    };
    await this.offline.enqueue(item);
    if (mutation.op === 'delete') {
      this.known.delete(`${kind}:${resourceId}`);
      return undefined;
    }
    const out = mutation.op === 'create' ? optimisticCreate(kind, resourceId, body ?? {}, at) : optimisticUpdate(kind, base ?? { id: resourceId }, body ?? {}, at);
    this.known.set(`${kind}:${resourceId}`, out);
    return out;
  }

  private async voice(path: string, opts: Options): Promise<unknown> {
    const form = opts.form;
    if (!form) return super.request('POST', path, opts);
    const key = newClientId();
    form.set('idempotency_key', key); // a replay of the same recording is answered, not re-processed
    const pending = await this.offline.pendingItems();
    if (this.offline.isOnline() && !pending.some((i) => i.kind === 'voice')) {
      try {
        return await withTimeout(super.request('POST', path, opts), VOICE_TIMEOUT_MS);
      } catch (err) {
        if (!isNetworkError(err)) throw err;
        this.offline.unreachable();
      }
    }
    const file = form.get('file');
    if (!(file instanceof Blob)) throw new HubError('No recording to keep for later', 0);
    const fields: Record<string, string> = {};
    form.forEach((v, k) => {
      if (k !== 'file' && typeof v === 'string') fields[k] = v;
    });
    const filename = typeof File !== 'undefined' && file instanceof File && file.name ? file.name : 'voice.webm';
    await this.offline.enqueue({
      kind: 'voice',
      op: 'upload',
      method: 'POST',
      path,
      resourceId: key,
      upload: { data: await file.arrayBuffer(), type: file.type || 'audio/webm', filename, fields },
      createdAt: this.offline.now(),
      status: 'pending',
      attempts: 0,
      label: path === '/money/voice' ? 'Voice money recording' : 'Voice note recording',
    });
    const queued: QueuedVoiceResult = { transcript: '', note: null, tasks: [], audio_key: '', expenses: [], incomes: [], queued: true };
    return queued;
  }
}
