import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Icon } from '../components/icons';
import { Button, Card, IconButton, SectionTitle, Spinner, StatusDot, Toggle, cx, inputClass } from '../components/ui';
import { ago } from '../lib/digest';
import { errorText, useHub } from '../lib/hub';
import {
  disablePush,
  enablePush,
  getSyncState,
  onPushChange,
  onPushReceived,
  permission as readPermission,
  promptInstall,
  pushEnv,
  reconnectPush,
  refreshLocal,
  savedLabel,
} from '../lib/pushClient';
import type { NotifyChannel, PushDevice } from '../lib/types';
import {
  IOS_STEPS,
  channelWarning,
  defaultDeviceLabel,
  deniedSteps,
  intentUrl,
  notReceivedChecks,
  pushView,
  type Permission,
  type PushAction,
  type PushHelp,
  type TestState,
} from '../lib/webpush';

export const MAIN_SITE = 'https://omnifree.ilyasouhnine.tech';

export function Help({ summary, children, open }: { summary: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="group rounded-row bg-ink-850 ring-1 ring-inset ring-ink-800" open={open}>
      <summary className="press flex min-h-[40px] cursor-pointer list-none items-center gap-2 px-3 text-[13px] text-ink-200">
        <Icon name="right" size={14} className="text-ink-500 transition-transform group-open:rotate-90" />
        {summary}
      </summary>
      <div className="space-y-1.5 px-3 pb-3 text-[12.5px] leading-snug text-ink-300">{children}</div>
    </details>
  );
}

export function Checklist({ items, ordered }: { items: string[]; ordered?: boolean }) {
  const Tag = ordered ? 'ol' : 'ul';
  return (
    <Tag className={cx('space-y-1 pl-4', ordered ? 'list-decimal' : 'list-disc')}>
      {items.map((t) => (
        <li key={t}>{t}</li>
      ))}
    </Tag>
  );
}

function DeviceRow({ d, here, onRemove, removing }: { d: PushDevice; here: boolean; onRemove?: () => void; removing?: boolean }) {
  const failedLast = !!d.last_failure_at && (!d.last_success_at || d.last_failure_at > d.last_success_at);
  return (
    <div className="flex items-center gap-2 py-1.5">
      <StatusDot tone={failedLast ? 'warn' : d.last_success_at ? 'ok' : 'off'} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] text-ink-100">
          {d.label || d.browser || 'Device'}
          {here && <span className="ml-1.5 text-[11px] text-signal">this device</span>}
        </p>
        <p className="truncate text-[11px] text-ink-500">
          {[d.browser, d.last_success_at && `ok ${ago(d.last_success_at)}`, failedLast && `failed ${ago(d.last_failure_at)}: ${d.last_error ?? ''}`]
            .filter(Boolean)
            .join(' · ') || `added ${ago(d.created_at)}`}
        </p>
      </div>
      {onRemove &&
        (removing ? <Spinner className="m-2" /> : <IconButton icon="trash" label={`Remove ${d.label || 'device'}`} className="h-9 w-9" onClick={onRemove} />)}
    </div>
  );
}

interface Props {
  channels: Record<NotifyChannel, boolean> | null;
  ntfyConfigured: boolean | null;
  onChannel: (channel: NotifyChannel, on: boolean) => void;
  savingChannel: NotifyChannel | null;
}

/** "App notifications": native Web Push on this device. Every state comes from pushView() (lib/webpush.ts). */
export default function AppPushCard({ channels, ntfyConfigured, onChannel, savingChannel }: Props) {
  const { client, settings, notify } = useHub();
  const sync = useSyncExternalStore(onPushChange, getSyncState);
  const [envTick, setEnvTick] = useState(0);
  const env = useMemo(() => pushEnv(), [envTick, sync]); // eslint-disable-line react-hooks/exhaustive-deps
  const [perm, setPerm] = useState<Permission | null>(() => readPermission());
  const [hub, setHub] = useState<{ phase: 'loading' | 'ok' | 'error'; configured: boolean; error: string | null }>({
    phase: 'loading', configured: false, error: null,
  });
  const [devices, setDevices] = useState<PushDevice[]>([]);
  const [localKnown, setLocalKnown] = useState(false);
  const [test, setTest] = useState<TestState>({ phase: 'idle' });
  const receivedTest = useRef(false); // the test push can arrive before the hub answers
  const [busy, setBusy] = useState<PushAction | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [label, setLabel] = useState(() => savedLabel() ?? defaultDeviceLabel(env.browser));

  const load = useCallback(async () => {
    setHub((h) => ({ ...h, phase: 'loading' }));
    try {
      const [cfg, listing] = await Promise.all([client.pushConfig(), client.pushSubscriptions()]);
      setHub({ phase: 'ok', configured: cfg.configured, error: cfg.error });
      setDevices(listing.subscriptions);
    } catch (err) {
      setHub((h) => ({ ...h, phase: 'error', error: errorText(err) })); // keep the last device list
    }
  }, [client]);

  useEffect(() => {
    void load();
    void refreshLocal().finally(() => setLocalKnown(true));
  }, [load]);

  // The automatic sync on app start may finish after this card loaded: reload the list when it does.
  useEffect(() => {
    if (!sync.syncing && sync.id && localKnown && !devices.some((d) => d.id === sync.id)) void load();
  }, [sync.syncing, sync.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const recheck = () => {
      if (document.visibilityState === 'visible') setPerm(readPermission());
    };
    document.addEventListener('visibilitychange', recheck);
    return () => document.removeEventListener('visibilitychange', recheck);
  }, []);

  useEffect(() => {
    const off = onPushReceived((msg) => {
      if (msg.tag !== 'omnifree-test') return;
      receivedTest.current = true;
      setTest((t) => (t.phase === 'done' ? { ...t, receivedHere: true } : t));
    });
    return off;
  }, []);

  const v = pushView({
    env,
    permission: perm,
    hub,
    local: { subscribed: !!sync.id, id: sync.id, syncing: sync.syncing || !localKnown, syncError: sync.error },
    devices,
    test,
  });

  const run = async (action: PushAction, fn: () => Promise<void>) => {
    setBusy(action);
    try {
      await fn();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const doAction = (a: PushAction) => {
    switch (a) {
      case 'enable':
        // requestPermission must be the first await after the click (inside enablePush).
        return run(a, async () => {
          const p = await enablePush(client, settings, label.trim() || null);
          setPerm(p);
          if (p === 'granted') notify('App notifications are on for this device', 'ok');
          await load();
        });
      case 'reconnect':
        return run(a, async () => {
          await reconnectPush(client, settings);
          notify('Reconnected', 'ok');
          await load();
        });
      case 'disable':
        return run(a, async () => {
          await disablePush(client);
          notify('App notifications are off for this device', 'ok');
          await load();
        });
      case 'test':
        return run(a, async () => {
          setTest({ phase: 'sending' });
          receivedTest.current = false;
          try {
            const res = await client.pushTest();
            const here = res.results.find((r) => r.id === sync.id) ?? null;
            setTest(() => ({ phase: 'done', sent: res.sent, failed: res.failed, here, receivedHere: receivedTest.current }));
            await load();
          } catch (err) {
            setTest({ phase: 'error', message: errorText(err) });
          }
        });
      case 'retry':
        return run(a, load);
      case 'check-again':
        setPerm(readPermission());
        return run(a, load);
      case 'install':
        return run(a, async () => {
          await promptInstall();
          setEnvTick((n) => n + 1);
        });
      default:
        return undefined;
    }
  };

  const remove = async (d: PushDevice) => {
    setRemoving(d.id);
    try {
      await client.pushUnsubscribe({ id: d.id });
      setDevices((list) => list.filter((x) => x.id !== d.id));
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setRemoving(null);
    }
  };

  const here = `${MAIN_SITE}/#/more/notifications`;
  const BUTTONS: Record<PushAction, ReactNode> = {
    enable: <Button key="enable" variant="primary" icon="bell" onClick={() => void doAction('enable')} disabled={!!busy}>{busy === 'enable' ? 'Enabling…' : 'Enable on this device'}</Button>,
    reconnect: <Button key="reconnect" variant="primary" icon="refresh" onClick={() => void doAction('reconnect')} disabled={!!busy}>{busy === 'reconnect' ? 'Reconnecting…' : 'Reconnect'}</Button>,
    test: <Button key="test" variant="primary" icon="send" onClick={() => void doAction('test')} disabled={!!busy}>{busy === 'test' ? 'Sending…' : 'Send test'}</Button>,
    disable: <Button key="disable" variant="danger" icon="x" onClick={() => void doAction('disable')} disabled={!!busy}>Disable on this device</Button>,
    retry: <Button key="retry" icon="refresh" onClick={() => void doAction('retry')} disabled={!!busy}>Retry</Button>,
    'check-again': <Button key="check" icon="refresh" onClick={() => void doAction('check-again')} disabled={!!busy}>Check again</Button>,
    install: <Button key="install" icon="download" onClick={() => void doAction('install')} disabled={!!busy}>Install app</Button>,
    'open-chrome': <a key="chrome" href={intentUrl(here, 'com.android.chrome')} className="press inline-flex min-h-[44px] items-center gap-2 rounded-full bg-ink-800 px-4 text-[15px] text-ink-100"><Icon name="external" size={18} />Open in Chrome</a>,
    'open-samsung': <a key="samsung" href={intentUrl(here, 'com.sec.android.app.sbrowser')} className="press inline-flex min-h-[44px] items-center gap-2 rounded-full bg-ink-800 px-4 text-[15px] text-ink-100"><Icon name="external" size={18} />Open in Samsung Internet</a>,
  };

  const HELP: Record<PushHelp, ReactNode> = {
    brave: (
      <Help key="brave" summary="Using Brave? Read this" open={v.kind === 'enable'}>
        <p>Brave on Android may not deliver web push notifications. Enable them and send a test: if nothing arrives within a minute, open <b className="text-ink-100">omnifree.ilyasouhnine.tech</b> in Chrome or Samsung Internet, add it to the home screen and enable notifications there. Your data is the same everywhere: it is stored on the server.</p>
        <p>In Brave, also check Settings → Privacy and security → “Use Google services for push messaging” (if your version has this setting); it must be on for pushes to arrive.</p>
      </Help>
    ),
    'ios-steps': (
      <Help key="ios" summary="How to add it to the Home Screen" open>
        <Checklist ordered items={IOS_STEPS} />
      </Help>
    ),
    'denied-steps': (
      <Help key="denied" summary="How to allow notifications again" open>
        <Checklist ordered items={deniedSteps(env.browser)} />
      </Help>
    ),
    'not-received': (
      <Help key="nr" summary="I didn’t receive it">
        <Checklist items={notReceivedChecks(env.browser, 'app')} />
      </Help>
    ),
    'install-optional': (
      <Help key="install" summary="Install as an app? (optional)">
        <p>On Android, notifications also work from the browser tab: installing is optional. Installed, OmniFree gets its own icon and notifications open it in its own window. {env.installPrompt ? 'Tap “Install app”.' : 'Use the browser menu → “Add to Home screen” or “Install app”.'}</p>
      </Help>
    ),
    unsupported: (
      <Help key="unsup" summary="What can I use instead?">
        <p>ntfy works on every phone: set it up in “Push notifications” below. Your data is on the server, so any browser shows the same OmniFree.</p>
      </Help>
    ),
  };

  const warning = channels && ntfyConfigured !== null
    ? channelWarning({ channels, ntfyConfigured, webpushConfigured: hub.configured, devices: devices.length })
    : null;
  const showDevices = hub.phase !== 'loading' || devices.length > 0;

  return (
    <div>
      <SectionTitle action={hub.phase === 'loading' ? <Spinner /> : <IconButton icon="refresh" label="Refresh app notifications" onClick={() => void load()} />}>
        App notifications
      </SectionTitle>
      <Card className="space-y-3 p-3">
        <div className="flex items-start gap-2.5">
          <span
            className={cx(
              'mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full',
              v.tone === 'ok' ? 'bg-signal/15 text-signal' : v.tone === 'warn' ? 'bg-amber-400/15 text-amber-400' : v.tone === 'bad' ? 'bg-danger-400/15 text-danger-400' : 'bg-ink-800 text-ink-400',
            )}
          >
            <Icon name={v.icon} size={16} />
          </span>
          <div className="min-w-0 flex-1" data-push-state={v.kind}>
            <p className="text-sm text-ink-100">{v.title}</p>
            {v.detail && <p className="mt-0.5 text-xs leading-snug text-ink-400">{v.detail}</p>}
          </div>
        </div>

        {v.actions.includes('enable') && (
          <label className="block space-y-1">
            <span className="text-xs text-ink-400">Name for this device</span>
            <input className={inputClass} value={label} maxLength={60} onChange={(e) => setLabel(e.target.value)} placeholder="S25 Brave" />
          </label>
        )}
        {v.actions.length > 0 && <div className="flex flex-wrap gap-2">{v.actions.map((a) => BUTTONS[a])}</div>}

        {v.test && (
          <div className={cx('rounded-row px-3 py-2 text-[13px]', v.test.tone === 'ok' ? 'bg-signal/10 text-signal' : v.test.tone === 'bad' ? 'bg-danger-400/10 text-danger-400' : 'bg-ink-850 text-ink-300')}>
            {v.test.text}
            {v.test.fix && <p className="mt-1 text-xs text-ink-300">{v.test.fix}</p>}
          </div>
        )}

        {warning && (
          <p className="flex items-start gap-2 rounded-row bg-amber-400/10 px-3 py-2 text-[13px] text-amber-400">
            <Icon name="flag" size={16} className="mt-0.5 shrink-0" />
            {warning}
          </p>
        )}

        {v.help.length > 0 && <div className="space-y-2">{v.help.map((h) => HELP[h])}</div>}

        {showDevices && (v.thisDevice || v.otherDevices.length > 0) && (
          <div className="divide-y divide-ink-800 border-t border-ink-800 pt-1">
            {v.thisDevice && <DeviceRow d={v.thisDevice} here />}
            {v.otherDevices.map((d) => (
              <DeviceRow key={d.id} d={d} here={false} removing={removing === d.id} onRemove={() => void remove(d)} />
            ))}
            {v.otherDevices.length > 0 && (
              <p className="pt-1.5 text-[11px] leading-snug text-ink-500">
                Removing a device stops pushes to it. If that browser still allows notifications, it registers again the next time OmniFree opens there; use “Disable on this device” on it to stop for good.
              </p>
            )}
          </div>
        )}

        <div className="divide-y divide-ink-800 border-t border-ink-800">
          {(['webpush', 'ntfy'] as NotifyChannel[]).map((c) => (
            <div key={c} className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <p className="text-sm text-ink-100">{c === 'webpush' ? 'App notifications' : 'ntfy'}</p>
                <p className="text-xs text-ink-400">
                  {c === 'webpush'
                    ? hub.phase === 'error' && !hub.configured
                      ? 'Unknown while the hub is unreachable'
                      : hub.configured ? `${devices.length} device${devices.length === 1 ? '' : 's'}` : 'Not set up on the server'
                    : ntfyConfigured ? 'Through the ntfy app' : 'Not set up (vault key NTFY_TOPIC)'}
                </p>
              </div>
              <Toggle
                checked={channels?.[c] ?? true}
                onChange={(on) => onChannel(c, on)}
                label={c === 'webpush' ? 'App notifications channel' : 'ntfy channel'}
                disabled={!channels || savingChannel === c}
              />
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
