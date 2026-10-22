// Pure helpers for the "App notifications" card (native Web Push): browser detection, key decoding and the card's
// state machine. No React, no I/O: the component renders what pushView() returns (see lib/pushClient.ts for I/O).

import type { PushDevice, PushTestResult } from './types';

// ------------------------------------------------------------------ keys

/** base64url (VAPID public key) -> bytes, for PushManager.subscribe({ applicationServerKey }). */
export function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const clean = base64.trim().replace(/=+$/, '');
  const padded = (clean + '='.repeat((4 - (clean.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** True when a subscription's applicationServerKey is the hub's current public key. */
export function sameKey(a: ArrayBuffer | Uint8Array | null | undefined, publicKey: string | null | undefined): boolean {
  if (!a || !publicKey) return false;
  const x = a instanceof Uint8Array ? a : new Uint8Array(a);
  const y = urlBase64ToUint8Array(publicKey);
  return x.length === y.length && x.every((v, i) => v === y[i]);
}

/** SHA-256 hex of the endpoint: the id the hub gives this subscription (hub_api/routers/push.py endpoint_id). */
export async function endpointId(endpoint: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

// ------------------------------------------------------------------ browser detection

export type BrowserName = 'brave' | 'samsung' | 'chrome' | 'edge' | 'firefox' | 'opera' | 'safari' | 'webview' | 'other';
export type OsName = 'android' | 'ios' | 'desktop';

export interface BrowserInfo {
  name: BrowserName;
  os: OsName;
  /** Opened from the home screen / as an installed app. */
  standalone: boolean;
  /** An in-app browser (Instagram, Facebook, Gmail, Android WebView...): no web push there. */
  webview: boolean;
}

export interface BrowserEnv {
  ua: string;
  /** `'brave' in navigator` (Brave hides in a Chrome user-agent). */
  brave?: boolean;
  standalone?: boolean;
  maxTouchPoints?: number;
}

const WEBVIEW_RE = /; wv\)|FBAN|FBAV|FB_IAB|Instagram|GSA\/|Line\/|MicroMessenger|Twitter|Snapchat|LinkedInApp|Pinterest/i;

export function detectBrowser(env: BrowserEnv): BrowserInfo {
  const ua = env.ua || '';
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (env.maxTouchPoints ?? 0) > 1);
  const os: OsName = ios ? 'ios' : /Android/.test(ua) ? 'android' : 'desktop';
  const webview = WEBVIEW_RE.test(ua);
  let name: BrowserName;
  if (webview) name = 'webview';
  else if (env.brave) name = 'brave';
  else if (/SamsungBrowser/.test(ua)) name = 'samsung';
  else if (/EdgA?\/|EdgiOS/.test(ua)) name = 'edge';
  else if (/Firefox|FxiOS/.test(ua)) name = 'firefox';
  else if (/OPR\/|OPiOS/.test(ua)) name = 'opera';
  else if (/Chrome\/|CriOS/.test(ua)) name = 'chrome';
  else if (/Safari\//.test(ua)) name = 'safari';
  else name = 'other';
  return { name, os, standalone: !!env.standalone, webview };
}

const BROWSER_LABEL: Record<BrowserName, string> = {
  brave: 'Brave', samsung: 'Samsung Internet', chrome: 'Chrome', edge: 'Edge', firefox: 'Firefox', opera: 'Opera',
  safari: 'Safari', webview: 'In-app browser', other: 'Browser',
};
const OS_LABEL: Record<OsName, string> = { android: 'Android', ios: 'iOS', desktop: 'Computer' };

/** "Brave (Android)": the browser hint sent to the hub (it cannot tell Brave from Chrome). */
export function browserHint(b: BrowserInfo): string {
  return `${BROWSER_LABEL[b.name]} (${OS_LABEL[b.os]})`;
}

/** Default device label, e.g. "Android · Brave" (the owner can rename it before enabling). */
export function defaultDeviceLabel(b: BrowserInfo): string {
  return `${OS_LABEL[b.os]} · ${BROWSER_LABEL[b.name]}${b.standalone ? ' app' : ''}`;
}

/** Android intent link that opens `pageUrl` in another installed browser (works from Chromium browsers). */
export function intentUrl(pageUrl: string, pkg: 'com.android.chrome' | 'com.sec.android.app.sbrowser'): string {
  const u = new URL(pageUrl);
  return `intent://${u.host}${u.pathname}${u.search}${u.hash}#Intent;scheme=${u.protocol.replace(':', '')};package=${pkg};end`;
}

// ------------------------------------------------------------------ help texts

export const IOS_STEPS = [
  'Open this site in Safari (iOS / iPadOS 16.4 or later).',
  'Tap the Share button, then “Add to Home Screen”.',
  'Open OmniFree from the new home-screen icon (not from Safari).',
  'Come back to this page and tap “Enable on this device”.',
];

/** How to re-allow notifications after "Block", for this browser first, then the Android system switch. */
export function deniedSteps(b: BrowserInfo): string[] {
  const steps: string[] = [];
  if (b.os === 'ios') {
    steps.push('iPhone Settings → Notifications → OmniFree → Allow Notifications.');
    return steps;
  }
  if (b.name === 'chrome' || b.name === 'edge' || b.name === 'opera') {
    steps.push(`${BROWSER_LABEL[b.name]}: tap the lock (or tune) icon left of the address → Permissions → Notifications → Allow.`);
  } else if (b.name === 'samsung') {
    steps.push('Samsung Internet: menu ☰ → Settings → Sites and downloads → Notifications → find this site → Allow.');
  } else if (b.name === 'brave') {
    steps.push('Brave: tap the lock icon left of the address (or the Brave shield) → Site settings → Notifications → Allow.');
  } else if (b.name === 'firefox') {
    steps.push('Firefox: tap the lock icon → Clear permissions (or Site settings → Notifications), then reload.');
  } else {
    steps.push('Open the site settings of this page (lock icon left of the address) → Notifications → Allow.');
  }
  if (b.os === 'android') {
    steps.push(`If the switch is greyed out: Android Settings → Apps → ${BROWSER_LABEL[b.name]} → Notifications → on.`);
  }
  steps.push('Then tap “Check again”.');
  return steps;
}

/** What to check when the hub says "sent" but nothing showed up. */
export function notReceivedChecks(b: BrowserInfo, channel: 'app' | 'ntfy'): string[] {
  if (channel === 'ntfy') {
    return [
      'The ntfy app is subscribed to exactly this topic (and the right server).',
      'Android Settings → Apps → ntfy → Notifications → on.',
      'Do Not Disturb / Modes is off, or lets ntfy through.',
      'Android Settings → Apps → ntfy → Battery → Unrestricted (otherwise Android may delay it).',
      'In ntfy, Settings → Connection: “instant delivery” for this topic delivers without Google services.',
    ];
  }
  const browser = BROWSER_LABEL[b.name];
  const checks = b.os === 'ios'
    ? [
        'Open OmniFree from the home-screen icon at least once after enabling.',
        'iPhone Settings → Notifications → OmniFree → Allow Notifications, and Focus is off.',
      ]
    : [
        `Android Settings → Apps → ${browser} → Notifications → on (the browser itself must be allowed).`,
        'Do Not Disturb / Modes is off, or lets the browser through.',
        `Android Settings → Apps → ${browser} → Battery → Unrestricted (a sleeping browser can miss pushes).`,
      ];
  if (b.name === 'brave') {
    checks.push('Brave: Settings → Privacy and security → “Use Google services for push messaging” must be on (if the setting exists in your version); Brave on Android may still not deliver web push.');
    checks.push('Fallback: open the site in Chrome or Samsung Internet, add it to the home screen and enable notifications there. Your data is on the server, so nothing is lost.');
  }
  checks.push('Send another test: pushes can take up to a minute when the phone is idle.');
  return checks;
}

/** A short fix for a failed test on one device, from the push service status. */
export function testFix(r: Pick<PushTestResult, 'status' | 'error' | 'removed'>): string {
  if (r.removed || r.status === 404 || r.status === 410) return 'The subscription expired and the hub removed it: tap “Reconnect”.';
  if (r.status === 401 || r.status === 403) return 'The push service rejected the server key (the VAPID keys changed?): tap “Reconnect”.';
  if (r.status === 413) return 'The message was too large for the push service.';
  if (r.status === 429) return 'The push service is rate-limiting: wait a minute and try again.';
  if (r.status && r.status >= 500) return 'The push service had a temporary error: try again in a minute.';
  return 'The hub could not reach the push service: try again; if it keeps failing, use ntfy meanwhile.';
}

// ------------------------------------------------------------------ the card's state machine

export type Permission = 'default' | 'granted' | 'denied';

export interface PushEnv {
  browser: BrowserInfo;
  hasServiceWorker: boolean;
  hasPushManager: boolean;
  hasNotification: boolean;
  secure: boolean;
  /** A `beforeinstallprompt` event was captured (Chromium on Android / desktop). */
  installPrompt: boolean;
}

export type TestState =
  | { phase: 'idle' }
  | { phase: 'sending' }
  | { phase: 'done'; sent: number; failed: number; here: PushTestResult | null; receivedHere: boolean }
  | { phase: 'error'; message: string };

export interface PushInputs {
  env: PushEnv;
  /** null while unknown / when the Notification API is missing. */
  permission: Permission | null;
  hub: { phase: 'loading' | 'ok' | 'error'; configured: boolean; error?: string | null };
  /** This browser's subscription (from the PushManager) and the last automatic sync. */
  local: { subscribed: boolean; id: string | null; syncing: boolean; syncError: string | null };
  /** The hub's list (kept from the last successful load when the hub is unreachable). */
  devices: PushDevice[];
  test: TestState;
}

export type PushStateKind =
  | 'webview'
  | 'unsupported'
  | 'ios-home-screen'
  | 'loading'
  | 'hub-offline'
  | 'hub-unconfigured'
  | 'denied'
  | 'enable'
  | 'reconnecting'
  | 'reconnect'
  | 'subscribed';

export type PushAction = 'enable' | 'reconnect' | 'test' | 'disable' | 'retry' | 'check-again' | 'install' | 'open-chrome' | 'open-samsung';
export type PushHelp = 'brave' | 'ios-steps' | 'denied-steps' | 'not-received' | 'install-optional' | 'unsupported';
export type Tone = 'ok' | 'warn' | 'bad' | 'off';

export interface TestLine {
  tone: Tone;
  text: string;
  fix?: string;
}

export interface PushView {
  kind: PushStateKind;
  tone: Tone;
  icon: 'bell' | 'check' | 'x' | 'lock' | 'refresh' | 'shield' | 'network' | 'settings' | 'external';
  title: string;
  detail: string | null;
  actions: PushAction[];
  help: PushHelp[];
  /** This device in the hub's list, when it is there. */
  thisDevice: PushDevice | null;
  /** Every other device, each with a remove action in the UI. */
  otherDevices: PushDevice[];
  test: TestLine | null;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function testLine(test: TestState, thisDevice: PushDevice | null): TestLine | null {
  switch (test.phase) {
    case 'idle':
      return null;
    case 'sending':
      return { tone: 'warn', text: 'Sending a test…' };
    case 'error':
      return { tone: 'bad', text: `Test failed: ${test.message}` };
    case 'done': {
      if (test.here && !test.here.ok) {
        return { tone: 'bad', text: `Not delivered to this device: ${test.here.error ?? `status ${test.here.status ?? '?'}`}`, fix: testFix(test.here) };
      }
      const total = `Sent to ${plural(test.sent, 'device')}${test.failed ? `, ${test.failed} failed` : ''}`;
      if (test.receivedHere) return { tone: 'ok', text: `${total}. Received on this device ✓` };
      if (test.sent === 0) return { tone: 'bad', text: test.failed ? `${total}.` : 'No device is subscribed yet.' };
      return { tone: 'ok', text: thisDevice ? `${total}. It should show up here within a minute.` : `${total}.` };
    }
  }
}

/** Everything the card shows, from what the browser and the hub report. Pure. */
export function pushView(i: PushInputs): PushView {
  const b = i.env.browser;
  const thisDevice = i.local.id ? i.devices.find((d) => d.id === i.local.id) ?? null : null;
  const otherDevices = i.devices.filter((d) => d !== thisDevice);
  const help: PushHelp[] = [];
  if (b.name === 'brave') help.push('brave');
  const androidLinks: PushAction[] = b.os === 'android' ? ['open-chrome', 'open-samsung'] : [];
  const base = { thisDevice, otherDevices, test: testLine(i.test, thisDevice) };
  const view = (v: Omit<PushView, 'thisDevice' | 'otherDevices' | 'test' | 'help'> & { help?: PushHelp[] }): PushView => ({
    ...base,
    ...v,
    help: [...(v.help ?? []), ...help.filter((h) => !(v.help ?? []).includes(h))],
  });

  // 1. In-app browsers and browsers without the APIs. iOS Safari outside the home screen has no PushManager:
  //    that is case 2, not "unsupported".
  if (b.webview) {
    return view({
      kind: 'webview', tone: 'bad', icon: 'external', title: 'In-app browser: notifications are not available here',
      detail: 'Apps like Instagram, Facebook or Gmail open links in their own browser, which cannot receive notifications. Open this page in Chrome or Samsung Internet (menu ⋮ → Open in browser).',
      actions: androidLinks, help: ['unsupported'],
    });
  }
  if (b.os === 'ios' && !b.standalone) {
    return view({
      kind: 'ios-home-screen', tone: 'warn', icon: 'settings', title: 'Add OmniFree to the Home Screen first',
      detail: 'On iPhone and iPad, notifications only work for sites added to the Home Screen (iOS 16.4 or later).',
      actions: [], help: ['ios-steps'],
    });
  }
  if (!i.env.secure || !i.env.hasServiceWorker || !i.env.hasPushManager || !i.env.hasNotification) {
    return view({
      kind: 'unsupported', tone: 'bad', icon: 'x', title: 'This browser cannot receive app notifications',
      detail: !i.env.secure
        ? 'Notifications need a secure (https) page.'
        : b.os === 'ios'
          ? 'Update to iOS 16.4 or later, then open OmniFree from the Home Screen icon.'
          : 'Open this site in an up-to-date Chrome or Samsung Internet. ntfy keeps working meanwhile.',
      actions: androidLinks, help: ['unsupported'],
    });
  }

  // 9. The hub: loading, unreachable (keep the local state), or without VAPID keys.
  if (i.hub.phase === 'loading' && i.devices.length === 0) {
    return view({ kind: 'loading', tone: 'off', icon: 'bell', title: 'Checking…', detail: null, actions: [] });
  }
  if (i.hub.phase === 'error') {
    const local = i.permission === 'granted' && i.local.subscribed
      ? 'This device is still subscribed in the browser; pushes arrive as soon as the hub is back.'
      : 'Nothing changed on this device.';
    return view({
      kind: 'hub-offline', tone: 'warn', icon: 'network', title: 'Can’t reach the hub',
      detail: `${i.hub.error ? `${i.hub.error}. ` : ''}${local}`, actions: ['retry'],
    });
  }
  if (!i.hub.configured) {
    return view({
      kind: 'hub-unconfigured', tone: 'off', icon: 'settings', title: 'Not set up on the server yet',
      detail: 'The hub has no VAPID keys. Generate them with “python -m automation.vapid”, store VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY in the vault and redeploy the hub. ntfy keeps working meanwhile.',
      actions: ['retry'],
    });
  }

  const install: PushAction[] = b.os === 'android' && !b.standalone && i.env.installPrompt ? ['install'] : [];
  const installHelp: PushHelp[] = b.os === 'android' && !b.standalone ? ['install-optional'] : [];

  // 5. Blocked.
  if (i.permission === 'denied') {
    return view({
      kind: 'denied', tone: 'bad', icon: 'lock', title: 'Notifications are blocked for this site',
      detail: 'The browser will not ask again: allow them in the site settings, then check again.',
      actions: ['check-again'], help: ['denied-steps'],
    });
  }

  // 4 / 10. Not asked yet (or never enabled here).
  if (i.permission !== 'granted') {
    return view({
      kind: 'enable', tone: 'off', icon: 'bell',
      title: otherDevices.length ? `On ${plural(otherDevices.length, 'other device')}, not this one` : 'Off on this device',
      detail: 'Get the same pushes as ntfy (reminders with “Fait ✓”, daily summaries) straight from OmniFree.',
      actions: ['enable', ...install], help: installHelp,
    });
  }

  // 7. Granted and registered on the hub.
  if (i.local.subscribed && thisDevice) {
    const failed = thisDevice.last_failure_at && (!thisDevice.last_success_at || thisDevice.last_failure_at > thisDevice.last_success_at);
    return view({
      kind: 'subscribed', tone: failed ? 'warn' : 'ok', icon: failed ? 'shield' : 'check',
      title: `On for this device${thisDevice.label ? ` (${thisDevice.label})` : ''}`,
      detail: failed ? `Last push failed: ${thisDevice.last_error ?? 'unknown error'}` : null,
      actions: ['test', 'disable', ...install], help: [...installHelp, ...(i.test.phase === 'done' ? (['not-received'] as PushHelp[]) : [])],
    });
  }

  // 8. Granted, but the subscription is missing, expired or unknown to the hub.
  if (i.local.syncing) {
    return view({ kind: 'reconnecting', tone: 'warn', icon: 'refresh', title: 'Reconnecting this device…', detail: null, actions: [] });
  }
  return view({
    kind: 'reconnect', tone: 'warn', icon: 'refresh', title: 'This device needs to reconnect',
    detail: i.local.syncError
      ? `Automatic reconnection failed: ${i.local.syncError}`
      : 'Notifications are allowed, but this device’s subscription is missing or expired on the hub.',
    actions: ['reconnect'],
  });
}

// ------------------------------------------------------------------ channels (ntfy + app)

export interface ChannelInputs {
  channels: { ntfy: boolean; webpush: boolean };
  ntfyConfigured: boolean;
  webpushConfigured: boolean;
  devices: number;
}

/** A warning when the switches and the setup leave no way for a push to arrive (null when fine). */
export function channelWarning(c: ChannelInputs): string | null {
  const ntfyOk = c.channels.ntfy && c.ntfyConfigured;
  const appOk = c.channels.webpush && c.webpushConfigured && c.devices > 0;
  if (!c.channels.ntfy && !c.channels.webpush) return 'Both channels are off: no notification will arrive.';
  if (ntfyOk || appOk) return null;
  const why = [
    c.channels.ntfy ? (c.ntfyConfigured ? null : 'ntfy is not set up') : 'ntfy is off',
    c.channels.webpush ? (!c.webpushConfigured ? 'app notifications are not set up on the server' : 'no device has app notifications on') : 'app notifications are off',
  ].filter(Boolean);
  return `No notification will arrive: ${why.join(' and ')}.`;
}
