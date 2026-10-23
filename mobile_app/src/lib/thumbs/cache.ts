// Thumbnail cache in IndexedDB, least recently used first out past a byte cap (~50 MB).
// Two stores: `meta` (id, size, last use: small, read whole for eviction) and `data` (the bytes).
// Entries are keyed by backend, key, etag/last_modified and size, so a changed file gets a new thumb.
// Falls back to memory when IndexedDB is missing or refuses to open.

import type { StoredObject } from '../types';

export const THUMB_CACHE_BYTES = 50 * 1024 * 1024;

/** What a thumbnail is: a picture, a text snippet (documents without one), or "nothing to show". */
export type ThumbKind = 'image' | 'text' | 'none';

export interface ThumbRecord {
  kind: ThumbKind;
  /** Picture bytes (kind 'image'). */
  data?: ArrayBuffer;
  mime?: string;
  /** Title or first words (kind 'text'). */
  text?: string;
  /** Kind 'none' after an error: when (retried a day later). */
  failedAt?: number;
  /** The error was the bucket refusing a cross-origin read. */
  cors?: boolean;
}

export interface ThumbMeta {
  id: string;
  size: number;
  used: number;
}

export function thumbId(obj: Pick<StoredObject, 'backend' | 'key' | 'etag' | 'last_modified' | 'size'>): string {
  return [obj.backend, obj.key, obj.etag || obj.last_modified || '', obj.size].join('|');
}

export function recordSize(rec: ThumbRecord): number {
  return (rec.data?.byteLength ?? 0) + (rec.text?.length ?? 0) * 2 + 64;
}

/** Ids to drop, least recently used first, so that `entries` fit within `cap` bytes. */
export function planEviction(entries: ThumbMeta[], cap: number): string[] {
  let total = entries.reduce((s, e) => s + e.size, 0);
  if (total <= cap) return [];
  const out: string[] = [];
  for (const e of [...entries].sort((a, b) => a.used - b.used || a.id.localeCompare(b.id))) {
    if (total <= cap) break;
    out.push(e.id);
    total -= e.size;
  }
  return out;
}

export interface ThumbStore {
  get(id: string): Promise<ThumbRecord | undefined>;
  put(id: string, rec: ThumbRecord): Promise<void>;
  /** Total bytes held. */
  usage(): Promise<number>;
  clear(): Promise<void>;
}

export class MemoryThumbStore implements ThumbStore {
  private items = new Map<string, { rec: ThumbRecord; meta: ThumbMeta }>();
  private clock = 0;

  constructor(
    private cap = THUMB_CACHE_BYTES,
    private now: () => number = () => Date.now() * 1000 + (this.clock++ % 1000),
  ) {}

  async get(id: string) {
    const hit = this.items.get(id);
    if (!hit) return undefined;
    hit.meta.used = this.now();
    return hit.rec;
  }

  async put(id: string, rec: ThumbRecord) {
    this.items.set(id, { rec, meta: { id, size: recordSize(rec), used: this.now() } });
    for (const drop of planEviction([...this.items.values()].map((v) => v.meta), this.cap)) this.items.delete(drop);
  }

  async usage() {
    let s = 0;
    for (const v of this.items.values()) s += v.meta.size;
    return s;
  }

  async clear() {
    this.items.clear();
  }
}

function req<T>(r: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new Error('aborted'));
  });
}

export class IdbThumbStore implements ThumbStore {
  private db: Promise<IDBDatabase>;
  /** A read only rewrites `used` when it is this stale (avoids a write per scroll). */
  private touchAfterMs = 60_000;
  private seq = 0;

  constructor(
    factory: IDBFactory,
    name = 'omnifree-thumbs',
    private cap = THUMB_CACHE_BYTES,
    private now: () => number = () => Date.now(),
  ) {
    this.db = new Promise((resolve, reject) => {
      const open = factory.open(name, 1);
      open.onupgradeneeded = () => {
        open.result.createObjectStore('meta', { keyPath: 'id' });
        open.result.createObjectStore('data');
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
      open.onblocked = () => reject(new Error('thumbnail cache blocked'));
    });
  }

  /** Strictly increasing use stamps, even within one millisecond (deterministic LRU order). */
  private stamp(): number {
    return this.now() * 1000 + (this.seq++ % 1000);
  }

  async get(id: string) {
    const db = await this.db;
    const tx = db.transaction(['meta', 'data'], 'readonly');
    const [meta, rec] = await Promise.all([
      req(tx.objectStore('meta').get(id) as IDBRequest<ThumbMeta | undefined>),
      req(tx.objectStore('data').get(id) as IDBRequest<ThumbRecord | undefined>),
    ]);
    if (!rec || !meta) return undefined;
    if (this.now() * 1000 - meta.used > this.touchAfterMs * 1000) {
      const w = db.transaction('meta', 'readwrite');
      w.objectStore('meta').put({ ...meta, used: this.stamp() });
      await done(w).catch(() => undefined);
    }
    return rec;
  }

  async put(id: string, rec: ThumbRecord) {
    const db = await this.db;
    const tx = db.transaction(['meta', 'data'], 'readwrite');
    tx.objectStore('data').put(rec, id);
    tx.objectStore('meta').put({ id, size: recordSize(rec), used: this.stamp() } satisfies ThumbMeta);
    await done(tx);
    await this.evict();
  }

  private async evict() {
    const db = await this.db;
    const all = await req(db.transaction('meta', 'readonly').objectStore('meta').getAll() as IDBRequest<ThumbMeta[]>);
    const drop = planEviction(all, this.cap);
    if (!drop.length) return;
    const tx = db.transaction(['meta', 'data'], 'readwrite');
    for (const id of drop) {
      tx.objectStore('meta').delete(id);
      tx.objectStore('data').delete(id);
    }
    await done(tx);
  }

  async usage() {
    const db = await this.db;
    const all = await req(db.transaction('meta', 'readonly').objectStore('meta').getAll() as IDBRequest<ThumbMeta[]>);
    return all.reduce((s, e) => s + e.size, 0);
  }

  async clear() {
    const db = await this.db;
    const tx = db.transaction(['meta', 'data'], 'readwrite');
    tx.objectStore('meta').clear();
    tx.objectStore('data').clear();
    await done(tx);
  }
}

let shared: ThumbStore | null = null;

/** The app's thumbnail store (IndexedDB, or memory when it is unavailable). */
export function thumbStore(): ThumbStore {
  if (shared) return shared;
  let store: ThumbStore;
  try {
    store = typeof indexedDB !== 'undefined' ? new IdbThumbStore(indexedDB) : new MemoryThumbStore();
  } catch {
    store = new MemoryThumbStore();
  }
  // Any IndexedDB failure (private mode, quota) degrades to memory for the session.
  const memory = new MemoryThumbStore();
  let failed = false;
  const guard = <T>(op: (s: ThumbStore) => Promise<T>): Promise<T> =>
    failed
      ? op(memory)
      : op(store).catch(() => {
          failed = true;
          return op(memory);
        });
  shared = {
    get: (id) => guard((s) => s.get(id)),
    put: (id, rec) => guard((s) => s.put(id, rec)),
    usage: () => guard((s) => s.usage()),
    clear: () => guard((s) => s.clear()),
  };
  return shared;
}
