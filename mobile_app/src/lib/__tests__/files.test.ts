import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient } from '../api';
import {
  allFolders,
  breadcrumbs,
  displayName,
  extensionOf,
  fileKind,
  folderOf,
  folderView,
  isIndexable,
  joinKey,
  relativeTime,
  sanitizeFileName,
  sanitizeFolder,
} from '../files';
import type { StoredObject } from '../types';

const obj = (key: string, size = 1): StoredObject => ({ key, size, backend: 'r2' });

describe('names', () => {
  it('splits keys into folder, display name and extension', () => {
    expect(displayName('docs/2026/Invoice March.PDF')).toBe('Invoice March');
    expect(extensionOf('docs/2026/Invoice March.PDF')).toBe('pdf');
    expect(folderOf('docs/2026/a.pdf')).toBe('docs/2026');
    expect(folderOf('a.pdf')).toBe('');
    expect(displayName('.env')).toBe('.env');
    expect(extensionOf('README')).toBe('');
  });

  it('detects kinds from the extension, then the content type', () => {
    expect(fileKind('a/photo.HEIC')).toBe('image');
    expect(fileKind('voice/x.webm')).toBe('video');
    expect(fileKind('blob', 'audio/webm')).toBe('audio');
    expect(fileKind('notes.md')).toBe('text');
    expect(fileKind('data.json')).toBe('code');
    expect(fileKind('x.bin', 'application/octet-stream')).toBe('other');
    expect(isIndexable('a.PDF')).toBe(true);
    expect(isIndexable('a.png')).toBe(false);
  });

  it('sanitises upload names without losing spaces', () => {
    expect(sanitizeFileName('  My   Trip  Photo.JPG ')).toBe('My Trip Photo.jpg');
    expect(sanitizeFileName('../../etc/passwd')).toBe('passwd');
    expect(sanitizeFileName('C:\\Users\\me\\report?.pdf')).toBe('report.pdf');
    expect(sanitizeFileName('...hidden')).toBe('hidden');
    expect(sanitizeFileName('<>:|')).toBe('file');
    expect(sanitizeFileName(`${'a'.repeat(200)}.txt`)).toHaveLength(120);
    expect(sanitizeFileName(`${'a'.repeat(200)}.txt`).endsWith('.txt')).toBe(true);
  });

  it('sanitises folders and joins keys', () => {
    expect(sanitizeFolder('/docs//../2026/ ./')).toBe('docs/2026');
    expect(joinKey('docs/', 'a.pdf')).toBe('docs/a.pdf');
    expect(joinKey('', 'a.pdf')).toBe('a.pdf');
  });
});

describe('folders', () => {
  const list = [obj('a.txt'), obj('docs/b.pdf', 10), obj('docs/2026/c.pdf', 5), obj('photos/d.jpg'), obj('docs/e.md')];

  it('shows sub-folders and direct files of a prefix', () => {
    const root = folderView(list, '');
    expect(root.files.map((f) => f.key)).toEqual(['a.txt']);
    expect(root.folders).toEqual([
      { name: 'docs', path: 'docs', count: 3, size: 16 },
      { name: 'photos', path: 'photos', count: 1, size: 1 },
    ]);
    const docs = folderView(list, 'docs/');
    expect(docs.files.map((f) => f.key)).toEqual(['docs/b.pdf', 'docs/e.md']);
    expect(docs.folders.map((f) => f.path)).toEqual(['docs/2026']);
  });

  it('lists every folder and builds breadcrumbs', () => {
    expect(allFolders(list)).toEqual(['docs', 'docs/2026', 'photos']);
    expect(breadcrumbs('docs/2026')).toEqual([
      { name: 'Drive', path: '' },
      { name: 'docs', path: 'docs' },
      { name: '2026', path: 'docs/2026' },
    ]);
  });

  it('formats relative times', () => {
    const now = new Date('2026-10-20T12:00:00Z');
    expect(relativeTime('2026-10-20T11:59:50Z', now)).toBe('just now');
    expect(relativeTime('2026-10-20T11:00:00Z', now)).toBe('1 h ago');
    expect(relativeTime('2026-10-20T06:00:00Z', now)).toBe('6 h ago');
    expect(relativeTime('2026-10-19T10:00:00Z', now)).toBe('yesterday');
    expect(relativeTime('2026-09-01T10:00:00Z', now)).toBe('1 Sept');
    expect(relativeTime(null, now)).toBe('');
  });
});

describe('drive client additions', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('locates a key and sends a renamed key with direct uploads', async () => {
    const seen: { url: string; body?: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url, body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined });
        return new Response(JSON.stringify({ key: 'voice/2026/10/a b.webm', size: 3, backend: 'backblaze_b2' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }),
    );
    const client = new HubClient({ hubUrl: 'https://h.example', token: 't' });
    expect((await client.driveLocate('voice/2026/10/a b.webm')).backend).toBe('backblaze_b2');
    expect(seen[0].url).toBe('https://h.example/drive/locate/voice/2026/10/a%20b.webm');
    await client.driveUploadUrl({ filename: 'x.mp4', size: 1, content_type: 'video/mp4', key: 'clips/x.mp4', provider: 'cloudflare_r2' });
    expect(seen[1].body).toMatchObject({ key: 'clips/x.mp4', provider: 'cloudflare_r2' });
  });
});
