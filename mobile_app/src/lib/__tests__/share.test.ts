// public/share-sw.js runs in the service worker (importScripts): load it in a sandbox with a fake `self`,
// plus the pure helpers of src/lib/share.ts that the Share screen uses.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';
import {
  aiPromptOf,
  extractUrl,
  isAudioFile,
  isReceiptFile,
  noteBodyOf,
  parseSharedMeta,
  shareActions,
  sharedText,
  sharedUrl,
  taskTitleOf,
  type SharedPayload,
} from '../share';

const SOURCE = readFileSync(join(__dirname, '..', '..', '..', 'public', 'share-sw.js'), 'utf8');
const ORIGIN = 'https://omnifree.ilyasouhnine.tech';

function load() {
  const listeners: Record<string, (e: unknown) => void> = {};
  const store = new Map<string, Response>();
  const cache = {
    keys: async () => [...store.keys()],
    delete: async (k: string) => store.delete(k),
    put: async (k: string, r: Response) => {
      store.set(k, r);
    },
  };
  const self = {
    location: new URL(ORIGIN),
    addEventListener: (type: string, fn: (e: unknown) => void) => {
      listeners[type] = fn;
    },
    caches: { open: async () => cache },
  } as Record<string, unknown>;
  runInNewContext(SOURCE, { self, URL, Response, Promise, Date, JSON, encodeURIComponent });
  const fetchEvent = async (request: { method: string; url: string; formData?: () => Promise<FormData> }) => {
    let answer = null as Promise<Response> | null;
    listeners.fetch({ request, respondWith: (p: Promise<Response>) => (answer = p) });
    return answer ? await answer : null;
  };
  return { store, fetchEvent, api: self.__omnifreeShare as Record<string, (...a: unknown[]) => unknown> };
}

const payload = (p: Partial<SharedPayload>): SharedPayload => ({ title: '', text: '', url: '', at: '', files: [], ...p });

describe('share-sw.js', () => {
  it('stashes a multipart share in Cache Storage and redirects to /#/share', async () => {
    const sw = load();
    const form = new FormData();
    form.append('title', 'Facture');
    form.append('text', 'Voir https://example.org/f');
    form.append('files', new File(['%PDF'], 'facture.pdf', { type: 'application/pdf' }));
    form.append('files', new File([''], 'empty.txt', { type: 'text/plain' })); // empty files are dropped
    const res = await sw.fetchEvent({ method: 'POST', url: `${ORIGIN}/share-target`, formData: async () => form });
    expect(res?.status).toBe(303);
    expect(res?.headers.get('location')).toBe(`${ORIGIN}/#/share`);
    const meta = await sw.store.get('/__share/meta.json')!.json();
    expect(meta).toMatchObject({
      title: 'Facture',
      text: 'Voir https://example.org/f',
      url: '',
      files: [{ path: '/__share/file/0', name: 'facture.pdf', type: 'application/pdf', size: 4 }],
    });
    expect(await sw.store.get('/__share/file/0')!.text()).toBe('%PDF');
    expect(sw.store.get('/__share/file/0')!.headers.get('X-Filename')).toBe('facture.pdf');
    expect(parseSharedMeta(meta)?.files).toHaveLength(1);
  });

  it('leaves every other request to Workbox and reports unreadable posts', async () => {
    const sw = load();
    expect(await sw.fetchEvent({ method: 'GET', url: `${ORIGIN}/share-target` })).toBeNull();
    expect(await sw.fetchEvent({ method: 'POST', url: `${ORIGIN}/notes` })).toBeNull();
    const res = await sw.fetchEvent({ method: 'POST', url: `${ORIGIN}/share-target`, formData: async () => Promise.reject(new Error('bad')) });
    expect(res?.headers.get('location')).toBe(`${ORIGIN}/#/share?error=1`);
  });
});

describe('share helpers', () => {
  it('finds the link in the text when the url field is empty (most Android apps)', () => {
    expect(extractUrl('Regarde ça https://youtu.be/abc?t=1). Top')).toBe('https://youtu.be/abc?t=1');
    expect(extractUrl('pas de lien')).toBe('');
    const p = payload({ title: 'Une vidéo', text: 'Regarde https://youtu.be/abc' });
    expect(sharedUrl(p)).toBe('https://youtu.be/abc');
    expect(sharedText(p)).toBe('Regarde');
    expect(sharedUrl(payload({ url: 'https://a.example', text: 'https://b.example' }))).toBe('https://a.example');
  });

  it('offers the actions that fit the payload', () => {
    const pdf = { path: '/x', name: 'facture.pdf', type: 'application/pdf', size: 1 };
    const photo = { path: '/y', name: 'IMG.jpg', type: 'image/jpeg', size: 1 };
    const memo = { path: '/z', name: 'memo.m4a', type: '', size: 1 };
    expect(shareActions(payload({ files: [pdf] }))).toEqual(['save', 'receipt', 'drive']);
    expect(shareActions(payload({ files: [photo] }))).toEqual(['save', 'receipt', 'drive']);
    expect(shareActions(payload({ files: [pdf, photo] }))).toEqual(['drive']);
    expect(shareActions(payload({ files: [memo] }))).toEqual(['voice', 'drive']);
    expect(shareActions(payload({ text: 'https://example.org' }))).toEqual(['save', 'link', 'note', 'task', 'ai']);
    expect(shareActions(payload({ text: 'acheter du pain' }))).toEqual(['save', 'note', 'task', 'ai']);
    expect(isReceiptFile({ name: 'scan.PDF', type: '' })).toBe(true);
    expect(isAudioFile({ name: 'a.txt', type: 'text/plain' })).toBe(false);
  });

  it('builds note bodies, task titles and the AI prompt', () => {
    const p = payload({ title: 'Recette', text: 'Le tajine du dimanche https://ex.org/t' });
    expect(noteBodyOf(p)).toBe('# Recette\n\nLe tajine du dimanche\n\nhttps://ex.org/t');
    expect(taskTitleOf(p)).toBe('Recette');
    expect(taskTitleOf(payload({ text: '\n  Rappeler Karim\nau sujet du devis' }))).toBe('Rappeler Karim');
    expect(taskTitleOf(payload({ url: 'https://ex.org' }))).toBe('https://ex.org');
    expect(taskTitleOf(payload({ title: 'x'.repeat(300) }))).toHaveLength(200);
    expect(aiPromptOf(p)).toBe('Recette\n\nLe tajine du dimanche\n\nhttps://ex.org/t');
    expect(parseSharedMeta({ title: '', text: '', files: [] })).toBeNull();
    expect(parseSharedMeta('nope')).toBeNull();
  });
});
