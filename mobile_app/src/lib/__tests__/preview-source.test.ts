import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError, type HubClient } from '../api';
import { HUB_READ_LIMIT, TooBigError, driveSource, objectUrl } from '../preview/source';
import type { StoredObject } from '../types';

const obj = (key: string, size = 10): StoredObject => ({ key, size, backend: 'nopresign' }) as StoredObject;

function fakeClient() {
  const downloads: string[] = [];
  const client = {
    drivePresign: async () => {
      throw new HubError('presign not supported', 400);
    },
    driveDownload: async (key: string) => {
      downloads.push(key);
      return new Blob([key]);
    },
  } as unknown as HubClient;
  return { client, downloads };
}

describe('object URLs through the hub fallback', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('revokes an expired blob URL when it is replaced', async () => {
    vi.useFakeTimers({ now: 1_000_000, toFake: ['Date'] });
    const revoked = vi.spyOn(URL, 'revokeObjectURL');
    const { client, downloads } = fakeClient();
    const first = await objectUrl(client, obj('a/expire.txt'));
    expect(await objectUrl(client, obj('a/expire.txt'))).toBe(first); // cached
    vi.setSystemTime(1_000_000 + 31 * 60_000);
    const second = await objectUrl(client, obj('a/expire.txt'));
    expect(second).not.toBe(first);
    expect(revoked).toHaveBeenCalledWith(first);
    expect(downloads).toEqual(['a/expire.txt', 'a/expire.txt']);
  });

  it('keeps a bounded number of blob URLs alive', async () => {
    const revoked = vi.spyOn(URL, 'revokeObjectURL');
    const { client } = fakeClient();
    const urls: string[] = [];
    for (let i = 0; i < 12; i++) urls.push(await objectUrl(client, obj(`many/${i}.txt`)));
    // The oldest ones were revoked, the most recent are still served from the cache.
    expect(revoked).toHaveBeenCalledWith(urls[0]);
    expect(revoked).not.toHaveBeenCalledWith(urls[11]);
    expect(await objectUrl(client, obj('many/11.txt'))).toBe(urls[11]);
  });

  it('refuses to download a large file into memory past blobMax', async () => {
    const { client, downloads } = fakeClient();
    await expect(objectUrl(client, obj('big.mp4', 50 * 1024 * 1024), undefined, 4 * 1024 * 1024)).rejects.toBeInstanceOf(TooBigError);
    expect(downloads).toEqual([]);
  });

  it('a Drive preview never pulls a file past the hub limit into a blob (too-big state instead)', async () => {
    const { client, downloads } = fakeClient();
    await expect(driveSource(client, obj('movie.mp4', HUB_READ_LIMIT + 1)).url()).rejects.toBeInstanceOf(TooBigError);
    expect(downloads).toEqual([]);
    expect(await driveSource(client, obj('small.mp3', 1000)).url()).toMatch(/^blob:/);
  });
});
