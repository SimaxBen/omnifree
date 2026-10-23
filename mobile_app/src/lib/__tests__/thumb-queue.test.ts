import { describe, expect, it, vi } from 'vitest';
import type { HubClient } from '../api';
import type { ThumbRecord } from '../thumbs/cache';
import { GENERATE_TIMEOUT_MS, cachedThumb, getThumb, queueStats } from '../thumbs';
import type { StoredObject } from '../types';

const client = {} as HubClient;
let n = 0;
const photo = (): StoredObject => ({ key: `q/${n++}.jpg`, size: 1000, backend: 'r2', etag: 'e', content_type: 'image/jpeg' }) as StoredObject;
const flush = () => new Promise((r) => setTimeout(r, 0));

/** A generator whose jobs finish when the test says so. */
function controllable() {
  const started: { key: string; signal: AbortSignal; finish: (rec: ThumbRecord) => void }[] = [];
  const gen = (_c: HubClient, obj: StoredObject, signal: AbortSignal) =>
    new Promise<ThumbRecord>((resolve) => started.push({ key: obj.key, signal, finish: resolve }));
  return { started, gen };
}

const TEXT = (t: string): ThumbRecord => ({ kind: 'text', text: t });

describe('thumbnail queue', () => {
  it('drops a waiting job once its tile scrolls away, before it ever starts', async () => {
    const { started, gen } = controllable();
    const [a, b, c] = [photo(), photo(), photo()];
    void getThumb(client, a, new AbortController().signal, gen);
    void getThumb(client, b, new AbortController().signal, gen);
    const away = new AbortController();
    const pc = getThumb(client, c, away.signal, gen);
    await flush();
    expect(started.map((s) => s.key)).toEqual([a.key, b.key]);
    expect(queueStats()).toEqual({ active: 2, waiting: 1 });
    away.abort();
    await expect(pc).rejects.toMatchObject({ name: 'AbortError' });
    started[0].finish(TEXT('a'));
    started[1].finish(TEXT('b'));
    await flush();
    await flush();
    expect(started.map((s) => s.key)).toEqual([a.key, b.key]); // c never generated
    expect(queueStats()).toEqual({ active: 0, waiting: 0 });
  });

  it('finishes and caches a job that already started even if its tile scrolls away', async () => {
    const { started, gen } = controllable();
    const p = photo();
    const away = new AbortController();
    const pending = getThumb(client, p, away.signal, gen);
    await flush();
    expect(started).toHaveLength(1);
    away.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(started[0].signal.aborted).toBe(false);
    started[0].finish(TEXT('kept'));
    await flush();
    await flush();
    expect(cachedThumb(p)).toEqual({ kind: 'text', text: 'kept' });
  });

  it('keeps a shared job alive while another tile still wants it', async () => {
    const { started, gen } = controllable();
    const p = photo();
    const one = new AbortController();
    const first = getThumb(client, p, one.signal, gen);
    const second = getThumb(client, p, new AbortController().signal, gen);
    await flush();
    one.abort();
    await expect(first).rejects.toMatchObject({ name: 'AbortError' });
    started[0].finish(TEXT('shared'));
    await expect(second).resolves.toEqual({ kind: 'text', text: 'shared' });
    expect(started).toHaveLength(1);
  });

  it('stops a generation that hangs, freeing its slot', async () => {
    vi.useFakeTimers();
    try {
      const { started, gen } = controllable();
      const p = photo();
      const pending = getThumb(client, p, new AbortController().signal, gen);
      await vi.advanceTimersByTimeAsync(0);
      expect(started).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(GENERATE_TIMEOUT_MS + 1);
      expect(started[0].signal.aborted).toBe(true);
      started[0].finish(TEXT('late')); // generators honour the signal; resolve to let the job end
      await vi.advanceTimersByTimeAsync(0);
      await pending;
      expect(queueStats().active).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
