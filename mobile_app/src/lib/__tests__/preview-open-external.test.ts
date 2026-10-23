// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError, type HubClient } from '../api';
import { openObjectExternally } from '../preview/source';
import type { StoredObject } from '../types';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('open externally', () => {
  it('saves a hub-fallback file instead of showing it from a same-origin blob: URL', async () => {
    const blobs = new Map<string, Blob>();
    let n = 0;
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: (b: Blob) => {
        const u = `blob:http://localhost/${n++}`;
        blobs.set(u, b);
        return u;
      },
      revokeObjectURL: () => undefined,
    }));
    vi.stubGlobal('fetch', async (u: string) => ({ blob: async () => blobs.get(u)! }));
    const tab = { opener: {}, location: { href: '' }, close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    const clicks: { href: string; download: string; target: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push({ href: this.href, download: this.download, target: this.target });
    });
    const client = {
      drivePresign: async () => {
        throw new HubError('no presign', 400);
      },
      driveDownload: async () => new Blob(['<script>steal(localStorage)</script>'], { type: 'text/html' }),
    } as unknown as HubClient;

    await openObjectExternally(client, { key: 'inbox/evil.html', backend: 'nopresign-open', size: 40 } as StoredObject);

    expect(tab.location.href).toBe(''); // the tab never shows the page
    expect(tab.close).toHaveBeenCalled();
    expect(clicks).toHaveLength(1);
    expect(clicks[0].download).toBe('evil.html');
    expect(clicks[0].target).toBe('');
    expect(blobs.get(clicks[0].href)?.type).toBe('application/octet-stream');
  });

  it('opens a presigned URL (the storage origin) in the tab', async () => {
    const tab = { opener: {}, location: { href: '' }, close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    const client = {
      drivePresign: async () => ({ url: 'https://bucket.example/doc.pdf?sig=1', expires_s: 3600 }),
    } as unknown as HubClient;
    await openObjectExternally(client, { key: 'doc.pdf', backend: 'presign-open', size: 1 } as StoredObject);
    expect(tab.location.href).toBe('https://bucket.example/doc.pdf?sig=1');
    expect(tab.opener).toBeNull();
  });
});
