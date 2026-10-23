// Thumbnail lookup for the Drive list/grid: memory -> IndexedDB -> generated (two at a time).
// Failures are remembered for a day (kind 'none' with `failedAt`) so scrolling never retries them;
// a storage CORS refusal is remembered as such, the image tile then loads the picture directly.

import type { HubClient } from '../api';
import { CorsBlockedError } from '../preview/source';
import type { StoredObject } from '../types';
import { thumbId, thumbStore, type ThumbRecord } from './cache';
import { generateThumb, thumbSourceOf } from './generate';

export { thumbSourceOf } from './generate';

export interface Thumb {
  kind: 'image' | 'text' | 'none';
  url?: string;
  text?: string;
  /** The bucket refused a cross-origin read: show the original image instead. */
  cors?: boolean;
}

const RETRY_FAILED_MS = 24 * 3600 * 1000;
const MEMORY_MAX = 400;
const memory = new Map<string, Thumb>();
const inflight = new Map<string, Promise<Thumb>>();

function remember(id: string, t: Thumb): Thumb {
  memory.delete(id);
  memory.set(id, t);
  if (memory.size > MEMORY_MAX) {
    const [oldId, old] = memory.entries().next().value as [string, Thumb];
    memory.delete(oldId);
    // A mounted <img> keeps its decoded picture after the URL is revoked.
    if (old.url) URL.revokeObjectURL(old.url);
  }
  return t;
}

function toThumb(rec: ThumbRecord): Thumb {
  if (rec.kind === 'image' && rec.data) return { kind: 'image', url: URL.createObjectURL(new Blob([rec.data], { type: rec.mime || 'image/webp' })) };
  if (rec.kind === 'text') return { kind: 'text', text: rec.text };
  return { kind: 'none', cors: rec.cors };
}

// ------------------------------------------------------------------ two at a time

const MAX_ACTIVE = 2;
let active = 0;
const waiting: { start: () => void; signal: AbortSignal }[] = [];

function pump() {
  while (active < MAX_ACTIVE && waiting.length) {
    const job = waiting.shift()!;
    if (job.signal.aborted) continue;
    active++;
    job.start();
  }
}

/** Runs `fn` once a slot is free (FIFO); jobs aborted while waiting never start. */
export function limit<T>(fn: () => Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new DOMException('aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort, { once: true });
    waiting.push({
      signal,
      start: () => {
        signal.removeEventListener('abort', onAbort);
        fn()
          .then(resolve, reject)
          .finally(() => {
            active--;
            pump();
          });
      },
    });
    pump();
  });
}

/** Test hook: queue state. */
export function queueStats(): { active: number; waiting: number } {
  return { active, waiting: waiting.length };
}

// ------------------------------------------------------------------ lookup

export function cachedThumb(obj: StoredObject): Thumb | undefined {
  return memory.get(thumbId(obj));
}

export function getThumb(client: HubClient, obj: StoredObject, signal: AbortSignal): Promise<Thumb> {
  const id = thumbId(obj);
  const hit = memory.get(id);
  if (hit) return Promise.resolve(hit);
  if (!thumbSourceOf(obj)) return Promise.resolve({ kind: 'none' });
  const running = inflight.get(id);
  if (running) return running;
  const store = thumbStore();
  const p = (async () => {
    const saved = await store.get(id).catch(() => undefined);
    if (saved && !(saved.kind === 'none' && saved.failedAt && Date.now() - saved.failedAt > RETRY_FAILED_MS)) {
      return remember(id, toThumb(saved));
    }
    let rec: ThumbRecord;
    try {
      rec = await limit(() => generateThumb(client, obj, signal), signal);
    } catch (err) {
      if (signal.aborted) throw err;
      rec = { kind: 'none', failedAt: Date.now(), cors: err instanceof CorsBlockedError };
    }
    await store.put(id, rec).catch(() => undefined);
    return remember(id, toThumb(rec));
  })();
  inflight.set(id, p);
  p.finally(() => inflight.delete(id)).catch(() => undefined);
  return p;
}
