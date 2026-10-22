// public/push-sw.js runs in the service worker (importScripts): load it in a sandbox with a fake `self`.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { describe, expect, it, vi } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', '..', '..', 'public', 'push-sw.js'), 'utf8');
const ORIGIN = 'https://omnifree.ilyasouhnine.tech';

interface Shown {
  title: string;
  options: Record<string, unknown>;
}

function load(origin = ORIGIN, fetchImpl: (url: string, init: RequestInit) => Promise<{ ok: boolean; status: number }> = async () => ({ ok: true, status: 200 })) {
  const listeners: Record<string, (e: unknown) => void> = {};
  const shown: Shown[] = [];
  const client = { url: `${origin}/#/home`, focus: vi.fn(async () => client), navigate: vi.fn(async () => client), postMessage: vi.fn() };
  const windows: (typeof client)[] = [];
  const self = {
    location: new URL(origin),
    addEventListener: (type: string, fn: (e: unknown) => void) => {
      listeners[type] = fn;
    },
    registration: {
      showNotification: vi.fn(async (title: string, options: Record<string, unknown>) => {
        shown.push({ title, options });
      }),
      getNotifications: vi.fn(async () => []),
      pushManager: { subscribe: vi.fn() },
    },
    clients: { matchAll: vi.fn(async () => windows), openWindow: vi.fn(async () => null) },
    atob: (s: string) => atob(s),
    indexedDB: undefined,
  } as Record<string, unknown>;
  const fetchMock = vi.fn(fetchImpl);
  runInNewContext(SOURCE, { self, URL, fetch: fetchMock, setTimeout: (fn: () => void) => fn(), Promise, Date, JSON, Object, Array, String, Uint8Array });
  const waits: Promise<unknown>[] = [];
  const dispatch = async (type: string, event: Record<string, unknown>) => {
    listeners[type]({ ...event, waitUntil: (p: Promise<unknown>) => waits.push(p) });
    await Promise.all(waits);
  };
  return { self, shown, client, windows, fetchMock, dispatch, api: self.__omnifreePush as Record<string, (...a: unknown[]) => unknown> };
}

const pushData = (obj: unknown) => ({ json: () => obj, text: () => JSON.stringify(obj) });

describe('push-sw.js', () => {
  it('registers the three handlers and shows the payload with icon, badge, tag, data and actions', async () => {
    const sw = load();
    const payload = {
      title: '⏰ Appeler Karim', body: '15:00 (dans 10 min)', tag: 'omnifree-reminder-t1', url: `${ORIGIN}/#/notes/tasks`,
      actions: [
        { action: 'done', title: 'Fait ✓', url: 'https://api.example/notify/done/t1?sig=abc' },
        { action: 'open', title: 'Tâches', url: `${ORIGIN}/#/notes/tasks` },
        { action: 'extra', title: 'Ignored', url: '/' },
      ],
    };
    await sw.dispatch('push', { data: pushData(payload) });
    expect(sw.shown).toHaveLength(1);
    const { title, options } = sw.shown[0];
    expect(title).toBe('⏰ Appeler Karim');
    expect(options).toMatchObject({
      body: '15:00 (dans 10 min)', icon: '/icons/icon-192.png', badge: '/icons/badge-96.png', tag: 'omnifree-reminder-t1', renotify: true,
      actions: [{ action: 'done', title: 'Fait ✓' }, { action: 'open', title: 'Tâches' }],
    });
    expect((options.data as { url: string }).url).toBe(`${ORIGIN}/#/notes/tasks`);
  });

  it('replaces the daily summary silently ("renotify": false)', async () => {
    const sw = load();
    await sw.dispatch('push', { data: pushData({ title: 'Aujourd’hui', body: 'x', tag: 'omnifree-summary', renotify: false }) });
    expect(sw.shown[0].options).toMatchObject({ tag: 'omnifree-summary', renotify: false });
  });

  it('copes with text or empty payloads', async () => {
    const sw = load();
    await sw.dispatch('push', { data: { json: () => { throw new Error('not json'); }, text: () => 'hello' } });
    await sw.dispatch('push', { data: null });
    expect(sw.shown.map((s) => [s.title, s.options.body])).toEqual([['OmniFree', 'hello'], ['OmniFree', '']]);
    expect(sw.shown[0].options.tag).toBeUndefined();
  });

  it('maps PWA links to this origin on a mirror, keeping the route', () => {
    const sw = load('https://omnifree.pages.dev');
    expect(sw.api.localUrl(`${ORIGIN}/#/notes/tasks`)).toBe('https://omnifree.pages.dev/#/notes/tasks');
    expect(sw.api.localUrl('https://elsewhere.example/page')).toBe('https://elsewhere.example/page');
    expect(Array.from(sw.api.urlBase64ToUint8Array('AQID') as Uint8Array)).toEqual([1, 2, 3]);
  });

  it('"Fait ✓" POSTs the signed link, then shows a confirmation', async () => {
    const sw = load();
    const notification = { title: '⏰ Appeler Karim', tag: 'omnifree-reminder-t1', close: vi.fn(), data: {
      url: `${ORIGIN}/#/notes/tasks`, actions: [{ action: 'done', title: 'Fait ✓', url: 'https://api.example/notify/done/t1?sig=abc' }],
    } };
    await sw.dispatch('notificationclick', { action: 'done', notification });
    expect(notification.close).toHaveBeenCalled();
    expect(sw.fetchMock).toHaveBeenCalledWith('https://api.example/notify/done/t1?sig=abc', expect.objectContaining({ method: 'POST' }));
    expect(sw.shown[0]).toMatchObject({ title: 'Fait ✓', options: { body: 'Appeler Karim', tag: 'omnifree-reminder-t1-done' } });
    expect(sw.self.clients as Record<string, unknown>).toBeTruthy();
  });

  it('"Fait ✓" failing opens the app at the tasks page', async () => {
    const sw = load(ORIGIN, async () => ({ ok: false, status: 404 }));
    const notification = { title: 't', tag: 'x', close: vi.fn(), data: { url: '/', actions: [{ action: 'done', title: 'Fait ✓', url: 'https://api.example/notify/done/t1?sig=bad' }] } };
    await sw.dispatch('notificationclick', { action: 'done', notification });
    expect(sw.shown).toHaveLength(0);
    expect((sw.self.clients as { openWindow: ReturnType<typeof vi.fn> }).openWindow).toHaveBeenCalledWith(`${ORIGIN}/#/notes/tasks`);
  });

  it('a tap focuses an open window and navigates it, or opens a new one', async () => {
    const sw = load();
    const notification = { title: 't', tag: 'x', close: vi.fn(), data: { url: `${ORIGIN}/#/more/expenses`, actions: [] } };
    await sw.dispatch('notificationclick', { action: '', notification });
    expect((sw.self.clients as { openWindow: ReturnType<typeof vi.fn> }).openWindow).toHaveBeenCalledWith(`${ORIGIN}/#/more/expenses`);

    sw.windows.push(sw.client);
    await sw.dispatch('notificationclick', { action: 'open', notification: { ...notification, data: { url: '/', actions: [{ action: 'open', title: 'Tâches', url: `${ORIGIN}/#/notes/tasks` }] } } });
    expect(sw.client.focus).toHaveBeenCalled();
    expect(sw.client.navigate).toHaveBeenCalledWith(`${ORIGIN}/#/notes/tasks`);
  });
});
