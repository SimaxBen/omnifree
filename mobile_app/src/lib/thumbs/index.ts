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

/** A generation that has started is never left running longer than this (a stalled fetch frees its slot). */
export const GENERATE_TIMEOUT_MS = 30_000;

interface Flight {
  promise: Promise<Thumb>;
  /** Aborts the job while it waits for a slot (not once it runs: a started thumbnail is finished and cached). */
  queue: AbortController;
  waiters: number;
}

const flights = new Map<string, Flight>();

function abortError(): DOMException {
  return new DOMException('aborted', 'AbortError');
}

/**
 * The thumbnail of `obj`. `signal` is the caller losing interest (the tile scrolled away): a job
 * still waiting for a slot is dropped once no caller wants it, so the queue only ever holds tiles
 * near the viewport; a job already running completes and is cached (no wasted half-downloads).
 */
export function getThumb(client: HubClient, obj: StoredObject, signal: AbortSignal, gen: typeof generateThumb = generateThumb): Promise<Thumb> {
  const id = thumbId(obj);
  const hit = memory.get(id);
  if (hit) return Promise.resolve(hit);
  if (!thumbSourceOf(obj)) return Promise.resolve({ kind: 'none' });
  if (signal.aborted) return Promise.reject(abortError());
  const flight = inflight(id, client, obj, gen);
  flight.waiters++;
  return new Promise<Thumb>((resolve, reject) => {
    let done = false;
    const onAbort = () => {
      if (done) return;
      done = true;
      flight.waiters--;
      if (flight.waiters <= 0) flight.queue.abort();
      reject(abortError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
    flight.promise.then(
      (t) => {
        signal.removeEventListener('abort', onAbort);
        if (done) return;
        done = true;
        resolve(t);
      },
      (err) => {
        signal.removeEventListener('abort', onAbort);
        if (done) return;
        done = true;
        reject(err);
      },
    );
  });
}

function inflight(id: string, client: HubClient, obj: StoredObject, gen: typeof generateThumb): Flight {
  const running = flights.get(id);
  if (running && !running.queue.signal.aborted) return running; // an aborted one is about to reject
  const queue = new AbortController();
  const store = thumbStore();
  const promise = (async () => {
    const saved = await store.get(id).catch(() => undefined);
    if (saved && !(saved.kind === 'none' && saved.failedAt && Date.now() - saved.failedAt > RETRY_FAILED_MS)) {
      return remember(id, toThumb(saved));
    }
    if (queue.signal.aborted) throw abortError();
    let rec: ThumbRecord;
    try {
      rec = await limit(() => {
        const run = new AbortController();
        const timer = setTimeout(() => run.abort(), GENERATE_TIMEOUT_MS);
        return gen(client, obj, run.signal).finally(() => clearTimeout(timer));
      }, queue.signal);
    } catch (err) {
      if (queue.signal.aborted && err instanceof DOMException && err.name === 'AbortError') throw err; // dropped from the queue
      rec = { kind: 'none', failedAt: Date.now(), cors: err instanceof CorsBlockedError };
    }
    await store.put(id, rec).catch(() => undefined);
    return remember(id, toThumb(rec));
  })();
  const flight: Flight = { promise, queue, waiters: 0 };
  flights.set(id, flight);
  promise
    .finally(() => {
      if (flights.get(id) === flight) flights.delete(id);
    })
    .catch(() => undefined);
  return flight;
}
