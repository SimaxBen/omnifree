import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it } from 'vitest';
import { IdbThumbStore, MemoryThumbStore, planEviction, recordSize, thumbId, type ThumbRecord } from '../thumbs/cache';
import { thumbDims } from '../thumbs/generate';

const pic = (bytes: number): ThumbRecord => ({ kind: 'image', data: new Uint8Array(bytes).buffer, mime: 'image/webp' });

describe('thumbnail cache', () => {
  it('keys thumbnails by backend, key, version and size', () => {
    const base = { backend: 'b2', key: 'a/b.jpg', size: 10, etag: 'e1', last_modified: '2026-01-01' };
    expect(thumbId(base)).toBe('b2|a/b.jpg|e1|10');
    expect(thumbId({ ...base, etag: null })).toBe('b2|a/b.jpg|2026-01-01|10');
    expect(thumbId({ ...base, size: 11 })).not.toBe(thumbId(base));
  });

  it('plans evictions least recently used first, until under the cap', () => {
    const entries = [
      { id: 'a', size: 40, used: 3 },
      { id: 'b', size: 40, used: 1 },
      { id: 'c', size: 40, used: 2 },
    ];
    expect(planEviction(entries, 200)).toEqual([]);
    expect(planEviction(entries, 80)).toEqual(['b']);
    expect(planEviction(entries, 50)).toEqual(['b', 'c']);
    expect(planEviction(entries, 0)).toEqual(['b', 'c', 'a']);
  });

  it('memory store: evicts the least recently read entry', async () => {
    let t = 0;
    const cap = recordSize(pic(100)) * 2;
    const s = new MemoryThumbStore(cap, () => ++t);
    await s.put('a', pic(100));
    await s.put('b', pic(100));
    await s.get('a'); // a is now more recent than b
    await s.put('c', pic(100));
    expect(await s.get('b')).toBeUndefined();
    expect(await s.get('a')).toBeDefined();
    expect(await s.get('c')).toBeDefined();
    expect(await s.usage()).toBe(cap);
  });

  it('IndexedDB store: persists, refreshes use on read, evicts LRU past the cap', async () => {
    let now = 1_000_000;
    const cap = recordSize(pic(1000)) * 2 + 10;
    const factory = new IDBFactory();
    const s = new IdbThumbStore(factory, 'thumbs-test', cap, () => now);
    await s.put('a', pic(1000));
    now += 1000;
    await s.put('b', pic(1000));
    now += 120_000; // past the touch threshold: reading a refreshes its last use
    const a = await s.get('a');
    expect(a?.kind).toBe('image');
    expect(a?.data?.byteLength).toBe(1000);
    now += 1000;
    await s.put('c', pic(1000));
    expect(await s.get('b')).toBeUndefined();
    expect(await s.get('a')).toBeDefined();
    expect(await s.get('c')).toBeDefined();
    expect(await s.usage()).toBeLessThanOrEqual(cap);

    // Small records (text tiles, failures) are cheap and survive.
    await s.put('t', { kind: 'text', text: 'Quarterly report' });
    expect((await s.get('t'))?.text).toBe('Quarterly report');

    // A second store on the same database sees the data (persistence across sessions).
    const again = new IdbThumbStore(factory, 'thumbs-test', cap, () => now);
    expect((await again.get('c'))?.kind).toBe('image');
    await again.clear();
    expect(await again.usage()).toBe(0);
  });

  it('sizes thumbnails to a 256 px shortest side, never upscaling', () => {
    expect(thumbDims(4000, 3000)).toEqual({ w: 341, h: 256 });
    expect(thumbDims(1080, 1920)).toEqual({ w: 256, h: 455 });
    expect(thumbDims(100, 50)).toEqual({ w: 100, h: 50 });
    expect(thumbDims(5000, 100)).toEqual({ w: 512, h: 10 });
  });
});
