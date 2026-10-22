// Browser side of native Web Push: permission, PushManager subscription, hub registration and the automatic
// re-sync on app start. The decisions (what the card shows) live in lib/webpush.ts; this file only does I/O.

import type { HubClient } from './api';
import type { HubSettings } from './settings';
import {
  browserHint,
  detectBrowser,
  endpointId,
  sameKey,
  urlBase64ToUint8Array,
  type BrowserInfo,
  type Permission,
  type PushEnv,
} from './webpush';

// ------------------------------------------------------------------ environment

export function currentBrowser(): BrowserInfo {
  const nav = typeof navigator === 'undefined' ? undefined : navigator;
  const standalone =
    typeof window !== 'undefined' &&
    ((typeof window.matchMedia === 'function' && window.matchMedia('(display-mode: standalone)').matches) ||
      (nav as Navigator & { standalone?: boolean } | undefined)?.standalone === true);
  return detectBrowser({
    ua: nav?.userAgent ?? '',
    brave: !!nav && 'brave' in nav,
    standalone,
    maxTouchPoints: nav?.maxTouchPoints ?? 0,
  });
}

export function pushEnv(): PushEnv {
  return {
    browser: currentBrowser(),
    hasServiceWorker: typeof navigator !== 'undefined' && 'serviceWorker' in navigator,
    hasPushManager: typeof window !== 'undefined' && 'PushManager' in window,
    hasNotification: typeof window !== 'undefined' && 'Notification' in window,
    secure: typeof window !== 'undefined' && window.isSecureContext,
    installPrompt: installPromptEvent !== null,
  };
}

export function permission(): Permission | null {
  return typeof Notification === 'undefined' ? null : (Notification.permission as Permission);
}

// ------------------------------------------------------------------ install prompt (Android Chromium)

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

let installPromptEvent: InstallPromptEvent | null = null;

/** Call once at startup: keeps `beforeinstallprompt` so the card can offer "Install app". */
export function captureInstallPrompt(): void {
  if (typeof window === 'undefined') return;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    installPromptEvent = e as InstallPromptEvent;
    emit();
  });
  window.addEventListener('appinstalled', () => {
    installPromptEvent = null;
    emit();
  });
}

export async function promptInstall(): Promise<boolean> {
  const e = installPromptEvent;
  if (!e) return false;
  installPromptEvent = null;
  await e.prompt();
  const choice = await e.userChoice.catch(() => ({ outcome: 'dismissed' as const }));
  emit();
  return choice.outcome === 'accepted';
}

// ------------------------------------------------------------------ tiny store for the sync status

export interface SyncState {
  syncing: boolean;
  error: string | null;
  /** Endpoint id of this browser's current subscription, null when none. */
  id: string | null;
  endpoint: string | null;
}

let syncState: SyncState = { syncing: false, error: null, id: null, endpoint: null };
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function setSync(patch: Partial<SyncState>): void {
  syncState = { ...syncState, ...patch };
  emit();
}

export function getSyncState(): SyncState {
  return syncState;
}

export function onPushChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Calls `cb` when the service worker shows a push (lets the card say "Received on this device ✓"). */
export function onPushReceived(cb: (msg: { tag: string | null; title: string }) => void): () => void {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return () => undefined;
  const handler = (e: MessageEvent) => {
    if (e.data && e.data.type === 'omnifree-push') cb({ tag: e.data.tag ?? null, title: e.data.title ?? '' });
  };
  navigator.serviceWorker.addEventListener('message', handler);
  return () => navigator.serviceWorker.removeEventListener('message', handler);
}

// ------------------------------------------------------------------ service worker config (IndexedDB)

/** What push-sw.js needs to re-register after `pushsubscriptionchange` (it cannot read localStorage). */
async function saveSwConfig(value: { hubUrl: string; token: string; publicKey: string | null; label: string | null; browser: string }): Promise<void> {
  if (typeof indexedDB === 'undefined') return;
  await new Promise<void>((resolve) => {
    const open = indexedDB.open('omnifree-push', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('kv');
    open.onerror = () => resolve();
    open.onsuccess = () => {
      const db = open.result;
      const tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put(value, 'config');
      tx.oncomplete = tx.onerror = () => {
        db.close();
        resolve();
      };
    };
  });
}

const LABEL_KEY = 'omnifree.push.label';

export function savedLabel(): string | null {
  try {
    return window.localStorage.getItem(LABEL_KEY);
  } catch {
    return null;
  }
}

function saveLabel(label: string | null): void {
  try {
    if (label) window.localStorage.setItem(LABEL_KEY, label);
    else window.localStorage.removeItem(LABEL_KEY);
  } catch {
    /* per-device convenience only */
  }
}

// ------------------------------------------------------------------ subscription

async function registration(timeoutMs = 8000): Promise<ServiceWorkerRegistration> {
  if (!('serviceWorker' in navigator)) throw new Error('service workers are not available');
  const ready = navigator.serviceWorker.ready;
  const timeout = new Promise<never>((_, reject) =>
    window.setTimeout(() => reject(new Error('the service worker is not active yet: reload the page')), timeoutMs),
  );
  return Promise.race([ready, timeout]);
}

export async function localSubscription(): Promise<PushSubscription | null> {
  try {
    const reg = await registration(3000);
    return await reg.pushManager.getSubscription();
  } catch {
    return null;
  }
}

/** Refreshes `syncState.id` from the PushManager (no network). */
export async function refreshLocal(): Promise<void> {
  const sub = await localSubscription();
  setSync({ id: sub ? await endpointId(sub.endpoint) : null, endpoint: sub?.endpoint ?? null });
}

async function subscribeFresh(reg: ServiceWorkerRegistration, publicKey: string, old: PushSubscription | null): Promise<PushSubscription> {
  if (old) await old.unsubscribe().catch(() => false);
  return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(publicKey) });
}

async function register(client: HubClient, settings: HubSettings, sub: PushSubscription, publicKey: string, label: string | null): Promise<void> {
  const browser = browserHint(currentBrowser());
  await client.pushSubscribe(sub.toJSON(), label, browser);
  await saveSwConfig({ hubUrl: settings.hubUrl, token: settings.token, publicKey, label, browser }).catch(() => undefined);
  setSync({ id: await endpointId(sub.endpoint), endpoint: sub.endpoint, error: null });
}

/**
 * "Enable on this device". Must run from a click: the permission request comes first, before any other await,
 * or Chrome ignores it. Returns the resulting permission.
 */
export async function enablePush(client: HubClient, settings: HubSettings, label: string | null): Promise<Permission> {
  const perm = (await Notification.requestPermission()) as Permission;
  if (perm !== 'granted') return perm;
  setSync({ syncing: true, error: null });
  try {
    const cfg = await client.pushConfig();
    if (!cfg.configured || !cfg.public_key) throw new Error(cfg.error || 'web push is not set up on the server');
    const reg = await registration();
    const existing = await reg.pushManager.getSubscription();
    const sub = existing && sameKey(existing.options.applicationServerKey, cfg.public_key)
      ? existing
      : await subscribeFresh(reg, cfg.public_key, existing);
    saveLabel(label);
    await register(client, settings, sub, cfg.public_key, label);
    return perm;
  } catch (err) {
    setSync({ error: err instanceof Error ? err.message : String(err) });
    throw err;
  } finally {
    setSync({ syncing: false });
  }
}

/** "Reconnect": a brand-new subscription (the old one may be dead on the push service), then register it. */
export async function reconnectPush(client: HubClient, settings: HubSettings): Promise<void> {
  setSync({ syncing: true, error: null });
  try {
    const cfg = await client.pushConfig();
    if (!cfg.configured || !cfg.public_key) throw new Error(cfg.error || 'web push is not set up on the server');
    const reg = await registration();
    const sub = await subscribeFresh(reg, cfg.public_key, await reg.pushManager.getSubscription());
    await register(client, settings, sub, cfg.public_key, savedLabel());
  } catch (err) {
    setSync({ error: err instanceof Error ? err.message : String(err) });
    throw err;
  } finally {
    setSync({ syncing: false });
  }
}

export async function disablePush(client: HubClient): Promise<void> {
  const sub = await localSubscription();
  if (sub) {
    await client.pushUnsubscribe({ endpoint: sub.endpoint }).catch(() => undefined);
    await sub.unsubscribe().catch(() => false);
  }
  setSync({ id: null, endpoint: null, error: null });
}

/**
 * On every app start (permission already granted): make sure this browser's subscription exists, uses the hub's
 * current key and is known to the hub. A subscription the hub no longer lists (expired and cleaned up after a
 * 404/410) is replaced by a fresh one. Never throws; the outcome is in getSyncState().
 */
export async function syncPush(client: HubClient, settings: HubSettings): Promise<void> {
  if (!settings.token || permission() !== 'granted' || !('PushManager' in window) || syncState.syncing) return;
  setSync({ syncing: true, error: null });
  try {
    const [cfg, listing] = await Promise.all([client.pushConfig(), client.pushSubscriptions()]);
    if (!cfg.configured || !cfg.public_key) return;
    const reg = await registration();
    let sub = await reg.pushManager.getSubscription();
    const known = sub ? await isListed(sub, listing.subscriptions.map((d) => d.id)) : false;
    if (sub && known && sameKey(sub.options.applicationServerKey, cfg.public_key)) {
      setSync({ id: await endpointId(sub.endpoint), endpoint: sub.endpoint });
      return;
    }
    sub = await subscribeFresh(reg, cfg.public_key, sub);
    await register(client, settings, sub, cfg.public_key, savedLabel());
  } catch (err) {
    setSync({ error: err instanceof Error ? err.message : String(err) });
  } finally {
    setSync({ syncing: false });
  }
}

async function isListed(sub: PushSubscription, ids: string[]): Promise<boolean> {
  return ids.includes(await endpointId(sub.endpoint));
}
