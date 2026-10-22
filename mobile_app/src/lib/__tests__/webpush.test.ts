import { describe, expect, it } from 'vitest';
import type { PushDevice, PushTestResult } from '../types';
import {
  browserHint,
  channelWarning,
  defaultDeviceLabel,
  deniedSteps,
  detectBrowser,
  endpointId,
  intentUrl,
  notReceivedChecks,
  pushView,
  sameKey,
  testFix,
  testLine,
  urlBase64ToUint8Array,
  type BrowserInfo,
  type PushInputs,
} from '../webpush';

const UA = {
  chromeAndroid: 'Mozilla/5.0 (Linux; Android 15; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36',
  samsung: 'Mozilla/5.0 (Linux; Android 15; SAMSUNG SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36',
  iphone: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  ipadDesktop: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
  instagram: 'Mozilla/5.0 (Linux; Android 15; SM-S931B Build/AP3A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0 Mobile Safari/537.36 Instagram 350.0',
  firefoxDesktop: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0',
  edge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 Edg/141.0',
};

describe('keys', () => {
  it('decodes a base64url VAPID public key (65-byte uncompressed point)', () => {
    const bytes = new Uint8Array(65).map((_, i) => (i === 0 ? 4 : (i * 37) % 256));
    const b64url = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    const out = urlBase64ToUint8Array(b64url);
    expect(out.length).toBe(65);
    expect(Array.from(out)).toEqual(Array.from(bytes));
    expect(Array.from(urlBase64ToUint8Array('AQID'))).toEqual([1, 2, 3]);
    expect(Array.from(urlBase64ToUint8Array('_-8'))).toEqual([255, 239]); // url-safe alphabet, no padding
    expect(sameKey(out.buffer, b64url)).toBe(true);
    expect(sameKey(new Uint8Array([1, 2, 3]), 'AQIE')).toBe(false);
    expect(sameKey(null, b64url)).toBe(false);
  });

  it('endpoint ids match the hub (SHA-256 hex)', async () => {
    expect(await endpointId('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  });
});

describe('browser detection', () => {
  it('tells Brave, Samsung Internet, Chrome, iOS, iPadOS, webviews and desktops apart', () => {
    expect(detectBrowser({ ua: UA.chromeAndroid, brave: true })).toEqual({ name: 'brave', os: 'android', standalone: false, webview: false });
    expect(detectBrowser({ ua: UA.chromeAndroid }).name).toBe('chrome');
    expect(detectBrowser({ ua: UA.samsung }).name).toBe('samsung');
    expect(detectBrowser({ ua: UA.iphone, standalone: true })).toEqual({ name: 'safari', os: 'ios', standalone: true, webview: false });
    expect(detectBrowser({ ua: UA.ipadDesktop, maxTouchPoints: 5 }).os).toBe('ios');
    expect(detectBrowser({ ua: UA.ipadDesktop, maxTouchPoints: 0 }).os).toBe('desktop');
    expect(detectBrowser({ ua: UA.instagram })).toMatchObject({ name: 'webview', webview: true });
    expect(detectBrowser({ ua: UA.firefoxDesktop })).toMatchObject({ name: 'firefox', os: 'desktop' });
    expect(detectBrowser({ ua: UA.edge }).name).toBe('edge');
  });

  it('builds labels, the hub hint and Android intent links', () => {
    const brave = detectBrowser({ ua: UA.chromeAndroid, brave: true });
    expect(browserHint(brave)).toBe('Brave (Android)');
    expect(defaultDeviceLabel(brave)).toBe('Android · Brave');
    expect(defaultDeviceLabel({ ...brave, name: 'chrome', standalone: true })).toBe('Android · Chrome app');
    expect(intentUrl('https://omnifree.ilyasouhnine.tech/#/more/notifications', 'com.android.chrome')).toBe(
      'intent://omnifree.ilyasouhnine.tech/#/more/notifications#Intent;scheme=https;package=com.android.chrome;end',
    );
  });
});

describe('help texts', () => {
  const b = (name: BrowserInfo['name'], os: BrowserInfo['os'] = 'android'): BrowserInfo => ({ name, os, standalone: false, webview: false });

  it('gives per-browser steps to re-allow notifications', () => {
    expect(deniedSteps(b('chrome'))[0]).toMatch(/Chrome: tap the lock .* Permissions → Notifications/);
    expect(deniedSteps(b('samsung'))[0]).toMatch(/Settings → Sites and downloads → Notifications/);
    expect(deniedSteps(b('brave'))[0]).toMatch(/Brave: .* Site settings/);
    expect(deniedSteps(b('brave'))[1]).toMatch(/Android Settings → Apps → Brave → Notifications/);
    expect(deniedSteps(b('brave')).at(-1)).toMatch(/Check again/);
    expect(deniedSteps(b('safari', 'ios'))).toEqual(['iPhone Settings → Notifications → OmniFree → Allow Notifications.']);
  });

  it('lists the "didn’t receive it" checks, with the Brave specifics', () => {
    const brave = notReceivedChecks(b('brave'), 'app').join('\n');
    expect(brave).toMatch(/Apps → Brave → Notifications/);
    expect(brave).toMatch(/Do Not Disturb/);
    expect(brave).toMatch(/Battery → Unrestricted/);
    expect(brave).toMatch(/Use Google services for push messaging/);
    expect(brave).toMatch(/may still not deliver/);
    expect(brave).toMatch(/Chrome or Samsung Internet/);
    expect(notReceivedChecks(b('chrome'), 'app').join('\n')).not.toMatch(/Google services/);
    expect(notReceivedChecks(b('chrome'), 'ntfy').join('\n')).toMatch(/Apps → ntfy → Notifications/);
  });

  it('explains a failed test from the push service status', () => {
    expect(testFix({ status: 410, error: 'gone', removed: true })).toMatch(/Reconnect/);
    expect(testFix({ status: 403, error: 'x', removed: false })).toMatch(/VAPID/);
    expect(testFix({ status: 429, error: 'x', removed: false })).toMatch(/rate-limiting/);
    expect(testFix({ status: 503, error: 'x', removed: false })).toMatch(/temporary/);
    expect(testFix({ status: null, error: 'unreachable', removed: false })).toMatch(/could not reach/);
  });
});

// ------------------------------------------------------------------ the state machine

const ANDROID_CHROME: BrowserInfo = { name: 'chrome', os: 'android', standalone: false, webview: false };
const dev = (id: string, extra: Partial<PushDevice> = {}): PushDevice => ({
  id, label: `dev ${id}`, browser: 'Chrome (Android)', push_service: 'fcm.googleapis.com', created_at: '2026-10-20T10:00:00Z',
  last_success_at: null, last_failure_at: null, last_error: null, ...extra,
});

function inputs(over: Partial<PushInputs> & { browser?: Partial<BrowserInfo>; envOver?: Partial<PushInputs['env']> } = {}): PushInputs {
  const { browser, envOver, ...rest } = over;
  return {
    env: { browser: { ...ANDROID_CHROME, ...browser }, hasServiceWorker: true, hasPushManager: true, hasNotification: true, secure: true, installPrompt: false, ...envOver },
    permission: 'default',
    hub: { phase: 'ok', configured: true, error: null },
    local: { subscribed: false, id: null, syncing: false, syncError: null },
    devices: [],
    test: { phase: 'idle' },
    ...rest,
  };
}

describe('pushView: every state of the card', () => {
  it('1a. in-app browser (Instagram...): not available, open in Chrome / Samsung Internet', () => {
    const v = pushView(inputs({ browser: { name: 'webview', webview: true } }));
    expect(v.kind).toBe('webview');
    expect(v.tone).toBe('bad');
    expect(v.actions).toEqual(['open-chrome', 'open-samsung']);
    expect(v.detail).toMatch(/Open in browser/);
  });

  it('1b. no Push API / service worker / https: unsupported, with a way out', () => {
    for (const envOver of [{ hasPushManager: false }, { hasServiceWorker: false }, { hasNotification: false }]) {
      const v = pushView(inputs({ envOver }));
      expect(v.kind).toBe('unsupported');
      expect(v.actions).toEqual(['open-chrome', 'open-samsung']);
      expect(v.help).toContain('unsupported');
    }
    expect(pushView(inputs({ envOver: { secure: false } })).detail).toMatch(/https/);
    const desktop = pushView(inputs({ browser: { os: 'desktop', name: 'other' }, envOver: { hasPushManager: false } }));
    expect(desktop.actions).toEqual([]);
    expect(desktop.help).toEqual(['unsupported']);
  });

  it('2. iOS Safari outside the home screen: add-to-home-screen steps (even without PushManager)', () => {
    const v = pushView(inputs({ browser: { name: 'safari', os: 'ios', standalone: false }, envOver: { hasPushManager: false } }));
    expect(v.kind).toBe('ios-home-screen');
    expect(v.help).toEqual(['ios-steps']);
    expect(v.detail).toMatch(/16\.4/);
    const installed = pushView(inputs({ browser: { name: 'safari', os: 'ios', standalone: true } }));
    expect(installed.kind).toBe('enable');
    const oldIos = pushView(inputs({ browser: { name: 'safari', os: 'ios', standalone: true }, envOver: { hasPushManager: false } }));
    expect(oldIos.kind).toBe('unsupported');
    expect(oldIos.detail).toMatch(/16\.4/);
  });

  it('3. Android not installed: install is optional; the prompt is offered when available', () => {
    const noPrompt = pushView(inputs());
    expect(noPrompt.help).toContain('install-optional');
    expect(noPrompt.actions).toEqual(['enable']);
    const prompt = pushView(inputs({ envOver: { installPrompt: true } }));
    expect(prompt.actions).toEqual(['enable', 'install']);
    const installed = pushView(inputs({ browser: { standalone: true }, envOver: { installPrompt: true } }));
    expect(installed.help).not.toContain('install-optional');
    expect(installed.actions).toEqual(['enable']);
  });

  it('4. permission "default": the Enable button', () => {
    const v = pushView(inputs());
    expect(v.kind).toBe('enable');
    expect(v.title).toBe('Off on this device');
    expect(v.actions[0]).toBe('enable');
  });

  it('5. permission "denied": per-browser steps and Check again', () => {
    const v = pushView(inputs({ permission: 'denied' }));
    expect(v.kind).toBe('denied');
    expect(v.tone).toBe('bad');
    expect(v.actions).toEqual(['check-again']);
    expect(v.help).toEqual(['denied-steps']);
  });

  it('6. after a test, the "didn’t receive it" checks are offered', () => {
    const base = { permission: 'granted' as const, local: { subscribed: true, id: 'a', syncing: false, syncError: null }, devices: [dev('a')] };
    expect(pushView(inputs(base)).help).not.toContain('not-received');
    const v = pushView(inputs({ ...base, test: { phase: 'done', sent: 1, failed: 0, here: null, receivedHere: false } }));
    expect(v.help).toContain('not-received');
  });

  it('7. granted and subscribed: device, last result, Test and Disable', () => {
    const ok = pushView(inputs({
      permission: 'granted', local: { subscribed: true, id: 'a', syncing: false, syncError: null },
      devices: [dev('a', { label: 'S25 Chrome', last_success_at: '2026-10-21T08:00:00Z' }), dev('b')],
    }));
    expect(ok.kind).toBe('subscribed');
    expect(ok.tone).toBe('ok');
    expect(ok.title).toBe('On for this device (S25 Chrome)');
    expect(ok.actions).toEqual(['test', 'disable']);
    expect(ok.thisDevice?.id).toBe('a');
    expect(ok.otherDevices.map((d) => d.id)).toEqual(['b']);
    const failing = pushView(inputs({
      permission: 'granted', local: { subscribed: true, id: 'a', syncing: false, syncError: null },
      devices: [dev('a', { last_success_at: '2026-10-20T08:00:00Z', last_failure_at: '2026-10-21T08:00:00Z', last_error: 'Push failed: 500' })],
    }));
    expect(failing.tone).toBe('warn');
    expect(failing.detail).toBe('Last push failed: Push failed: 500');
  });

  it('8. granted but missing / expired on the hub: reconnecting, then Reconnect when that failed', () => {
    const syncing = pushView(inputs({ permission: 'granted', local: { subscribed: false, id: null, syncing: true, syncError: null } }));
    expect(syncing.kind).toBe('reconnecting');
    expect(syncing.actions).toEqual([]);
    const failed = pushView(inputs({ permission: 'granted', local: { subscribed: false, id: null, syncing: false, syncError: 'AbortError' } }));
    expect(failed.kind).toBe('reconnect');
    expect(failed.actions).toEqual(['reconnect']);
    expect(failed.detail).toMatch(/AbortError/);
    // Subscribed in the browser, but the hub removed it (404/410 cleanup).
    const removed = pushView(inputs({ permission: 'granted', local: { subscribed: true, id: 'gone', syncing: false, syncError: null }, devices: [dev('other')] }));
    expect(removed.kind).toBe('reconnect');
    expect(removed.otherDevices.map((d) => d.id)).toEqual(['other']);
  });

  it('9a. hub not configured: "not set up on the server yet"', () => {
    const v = pushView(inputs({ hub: { phase: 'ok', configured: false, error: 'vault key VAPID_PRIVATE_KEY is not set' } }));
    expect(v.kind).toBe('hub-unconfigured');
    expect(v.title).toBe('Not set up on the server yet');
    expect(v.detail).toMatch(/automation\.vapid/);
    expect(v.actions).toEqual(['retry']);
  });

  it('9b. hub unreachable: Retry, keeping the local state and the last device list', () => {
    const v = pushView(inputs({
      hub: { phase: 'error', configured: true, error: 'Cannot reach hub' }, permission: 'granted',
      local: { subscribed: true, id: 'a', syncing: false, syncError: null }, devices: [dev('a')],
    }));
    expect(v.kind).toBe('hub-offline');
    expect(v.actions).toEqual(['retry']);
    expect(v.detail).toMatch(/still subscribed in the browser/);
    expect(v.thisDevice?.id).toBe('a');
    expect(pushView(inputs({ hub: { phase: 'error', configured: false, error: null } })).detail).toMatch(/Nothing changed/);
    expect(pushView(inputs({ hub: { phase: 'loading', configured: false, error: null } })).kind).toBe('loading');
    // Reloading with a known list keeps showing the last state instead of "Checking…".
    expect(pushView(inputs({ hub: { phase: 'loading', configured: true, error: null }, devices: [dev('x')] })).kind).toBe('enable');
  });

  it('10. on other devices but not this one: list them, Enable here', () => {
    const v = pushView(inputs({ devices: [dev('a'), dev('b')] }));
    expect(v.kind).toBe('enable');
    expect(v.title).toBe('On 2 other devices, not this one');
    expect(v.otherDevices).toHaveLength(2);
    expect(v.actions).toContain('enable');
    expect(pushView(inputs({ devices: [dev('a')] })).title).toBe('On 1 other device, not this one');
  });

  it('11. Brave: the possible non-delivery note, in every state', () => {
    for (const over of [{}, { permission: 'denied' as const }, { envOver: { hasPushManager: false } }]) {
      expect(pushView(inputs({ ...over, browser: { name: 'brave' } })).help).toContain('brave');
    }
    expect(pushView(inputs()).help).not.toContain('brave');
  });

  it('12. a test: pending, then "Sent to N devices", or this device’s error and the fix', () => {
    expect(testLine({ phase: 'idle' }, null)).toBeNull();
    expect(testLine({ phase: 'sending' }, null)).toEqual({ tone: 'warn', text: 'Sending a test…' });
    expect(testLine({ phase: 'done', sent: 2, failed: 0, here: null, receivedHere: false }, dev('a'))?.text).toBe(
      'Sent to 2 devices. It should show up here within a minute.',
    );
    expect(testLine({ phase: 'done', sent: 1, failed: 1, here: null, receivedHere: true }, dev('a'))?.text).toBe(
      'Sent to 1 device, 1 failed. Received on this device ✓',
    );
    expect(testLine({ phase: 'done', sent: 0, failed: 0, here: null, receivedHere: false }, null)?.text).toBe('No device is subscribed yet.');
    const here: PushTestResult = { id: 'a', label: 'S25', browser: null, ok: false, status: 410, error: 'Push failed: 410 Gone', removed: true };
    const line = testLine({ phase: 'done', sent: 1, failed: 1, here, receivedHere: false }, dev('a'));
    expect(line).toMatchObject({ tone: 'bad', text: 'Not delivered to this device: Push failed: 410 Gone' });
    expect(line?.fix).toMatch(/Reconnect/);
    expect(testLine({ phase: 'error', message: '409 not set up' }, null)).toEqual({ tone: 'bad', text: 'Test failed: 409 not set up' });
    const v = pushView(inputs({
      permission: 'granted', local: { subscribed: true, id: 'a', syncing: false, syncError: null }, devices: [dev('a')], test: { phase: 'sending' },
    }));
    expect(v.test?.text).toBe('Sending a test…');
  });

  it('13. both channels off (or nothing able to deliver): a warning', () => {
    const ok = { ntfyConfigured: true, webpushConfigured: true, devices: 1 };
    expect(channelWarning({ ...ok, channels: { ntfy: true, webpush: true } })).toBeNull();
    expect(channelWarning({ ...ok, channels: { ntfy: false, webpush: false } })).toBe('Both channels are off: no notification will arrive.');
    expect(channelWarning({ ...ok, channels: { ntfy: false, webpush: true } })).toBeNull();
    expect(channelWarning({ ...ok, devices: 0, channels: { ntfy: false, webpush: true } })).toBe(
      'No notification will arrive: ntfy is off and no device has app notifications on.',
    );
    expect(channelWarning({ ntfyConfigured: false, webpushConfigured: false, devices: 0, channels: { ntfy: true, webpush: true } })).toBe(
      'No notification will arrive: ntfy is not set up and app notifications are not set up on the server.',
    );
  });

  it('14. ntfy: its own "didn’t receive it" checks', () => {
    const checks = notReceivedChecks(ANDROID_CHROME, 'ntfy');
    expect(checks.some((c) => c.includes('subscribed to exactly this topic'))).toBe(true);
    expect(checks.some((c) => c.includes('Battery'))).toBe(true);
  });
});
