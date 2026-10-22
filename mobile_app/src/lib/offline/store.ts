// Offline mode, storage: the read cache (last good GET bodies) and the write queue, in IndexedDB.
// Falls back to memory when IndexedDB is missing or refuses to open (private windows, previews):
// the app keeps working, it just forgets on reload.

import type { QueueItem } from './model';

export interface CacheEntry {
  key: string;
  body: unknown;
  savedAt: number;
}

export interface OfflineStore {
  cacheGet(key: string): Promise<CacheEntry | undefined>;
  cachePut(key: string, body: unknown, savedAt: number): Promise<void>;
  cacheAll(): Promise<CacheEntry[]>;
  queueAll(): Promise<QueueItem[]>;
  /** Adds an entry and answers its sequence number (enqueue order). */
  queueAdd(item: QueueItem): Promise<number>;
  queuePut(item: QueueItem): Promise<void>;
  queueDelete(seq: number): Promise<void>;
  clear(): Promise<void>;
}

/** Cache entries kept; the oldest go first. */
export const CACHE_LIMIT = 80;

export class MemoryStore implements OfflineStore {
  private cache = new Map<string, CacheEntry>();
  private queue = new Map<number, QueueItem>();
  private next = 1;

  async cacheGet(key: string) {
    return this.cache.get(key);
  }

  async cachePut(key: string, body: unknown, savedAt: number) {
    this.cache.set(key, { key, body, savedAt });
    if (this.cache.size > CACHE_LIMIT) {
      const oldest = [...this.cache.values()].sort((a, b) => a.savedAt - b.savedAt).slice(0, this.cache.size - CACHE_LIMIT);
      for (const e of oldest) this.cache.delete(e.key);
    }
  }

  async cacheAll() {
    return [...this.cache.values()];
  }

  async queueAll() {
    return [...this.queue.values()].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  }

  async queueAdd(item: QueueItem) {
    const seq = this.next++;
    this.queue.set(seq, { ...item, seq });
    return seq;
  }

  async queuePut(item: QueueItem) {
    if (item.seq !== undefined) this.queue.set(item.seq, item);
  }

  async queueDelete(seq: number) {
    this.queue.delete(seq);
  }

  async clear() {
    this.cache.clear();
    this.queue.clear();
  }
}

const DB_VERSION = 1;

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
    tx.onabort = () => reject(tx.error ?? new Error('transaction aborted'));
  });
}

export class IdbStore implements OfflineStore {
  private db: Promise<IDBDatabase | null> | null = null;
  private fallback = new MemoryStore();

  constructor(
    private readonly factory: IDBFactory | undefined = typeof indexedDB === 'undefined' ? undefined : indexedDB,
    private readonly name = 'omnifree-offline',
  ) {}

  private open(): Promise<IDBDatabase | null> {
    if (!this.db) {
      this.db = new Promise<IDBDatabase | null>((resolve) => {
        if (!this.factory) return resolve(null);
        let r: IDBOpenDBRequest;
        try {
          r = this.factory.open(this.name, DB_VERSION);
        } catch {
          return resolve(null);
        }
        r.onupgradeneeded = () => {
          const db = r.result;
          if (!db.objectStoreNames.contains('cache')) db.createObjectStore('cache', { keyPath: 'key' });
          if (!db.objectStoreNames.contains('queue')) db.createObjectStore('queue', { keyPath: 'seq', autoIncrement: true });
        };
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => resolve(null);
        r.onblocked = () => resolve(null);
      });
    }
    return this.db;
  }

  private async run<T>(store: 'cache' | 'queue', mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>, mem: (m: MemoryStore) => Promise<T>): Promise<T> {
    const db = await this.open();
    if (!db) return mem(this.fallback);
    const tx = db.transaction(store, mode);
    const [result] = await Promise.all([req(fn(tx.objectStore(store))), done(tx)]);
    return result;
  }

  cacheGet(key: string) {
    return this.run<CacheEntry | undefined>('cache', 'readonly', (s) => s.get(key), (m) => m.cacheGet(key));
  }

  async cachePut(key: string, body: unknown, savedAt: number) {
    await this.run('cache', 'readwrite', (s) => s.put({ key, body, savedAt }), (m) => m.cachePut(key, body, savedAt).then(() => key));
    const all = await this.cacheAll();
    if (all.length > CACHE_LIMIT) {
      const oldest = all.sort((a, b) => a.savedAt - b.savedAt).slice(0, all.length - CACHE_LIMIT);
      for (const e of oldest) await this.run<undefined>('cache', 'readwrite', (s) => s.delete(e.key), async () => undefined);
    }
  }

  cacheAll() {
    return this.run<CacheEntry[]>('cache', 'readonly', (s) => s.getAll(), (m) => m.cacheAll());
  }

  async queueAll() {
    const items = await this.run<QueueItem[]>('queue', 'readonly', (s) => s.getAll(), (m) => m.queueAll());
    return items.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  }

  async queueAdd(item: QueueItem) {
    const { seq: _drop, ...rest } = item;
    const key = await this.run<IDBValidKey>('queue', 'readwrite', (s) => s.add(rest), (m) => m.queueAdd(item));
    return Number(key);
  }

  async queuePut(item: QueueItem) {
    if (item.seq === undefined) return;
    await this.run('queue', 'readwrite', (s) => s.put(item), (m) => m.queuePut(item).then(() => 0));
  }

  async queueDelete(seq: number) {
    await this.run<undefined>('queue', 'readwrite', (s) => s.delete(seq), (m) => m.queueDelete(seq).then(() => undefined));
  }

  async clear() {
    await this.run<undefined>('cache', 'readwrite', (s) => s.clear(), (m) => m.clear().then(() => undefined));
    await this.run<undefined>('queue', 'readwrite', (s) => s.clear(), async () => undefined);
  }
}
