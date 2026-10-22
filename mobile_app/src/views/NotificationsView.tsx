import { useState, type ReactNode } from 'react';
import { Icon } from '../components/icons';
import { Badge, Button, Card, ErrorNote, IconButton, SectionTitle, SkeletonList, Spinner, StatusDot, Toggle, cx } from '../components/ui';
import { ago } from '../lib/digest';
import { copyText } from '../lib/dom';
import { errorText, useHub, useHubData } from '../lib/hub';
import { useNav } from '../lib/nav';
import { EVENT_KINDS, NOTIFY_KINDS, maskFeedUrl, maskSecret, ntfySubscribeLink } from '../lib/notify';
import { currentBrowser } from '../lib/pushClient';
import type { GCalCalendar, NotifyChannel, NotifyChannelResults, NotifyKind, NotifyResult, NotifyRunKind } from '../lib/types';
import { notReceivedChecks } from '../lib/webpush';
import AppPushCard, { Checklist, Help } from './AppPushCard';

const PLAY_URL = 'https://play.google.com/store/apps/details?id=io.heckel.ntfy';
const APPSTORE_URL = 'https://apps.apple.com/app/ntfy/id1625396347';

function SecretRow({ label, value, masked, onCopy }: { label: string; value: string; masked: string; onCopy: () => void }) {
  const [reveal, setReveal] = useState(false);
  return (
    <div className="space-y-1">
      <p className="text-xs text-ink-400">{label}</p>
      <div className="flex items-center gap-1 rounded-row bg-ink-850 pl-3 ring-1 ring-inset ring-ink-700">
        <span className={cx('min-w-0 flex-1 break-all py-2 font-mono text-[13px]', reveal ? 'text-ink-100' : 'text-ink-400')}>
          {reveal ? value : masked}
        </span>
        <IconButton icon="eye" label={reveal ? `Hide ${label}` : `Show ${label}`} onClick={() => setReveal((v) => !v)} className={cx('h-10 w-10', reveal && 'text-signal')} />
        <IconButton icon="copy" label={`Copy ${label}`} onClick={onCopy} className="h-10 w-10" />
      </div>
    </div>
  );
}

function Steps({ items }: { items: ReactNode[] }) {
  return (
    <ol className="space-y-2">
      {items.map((s, i) => (
        <li key={i} className="flex gap-3 text-[13px] leading-snug text-ink-300">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ink-800 font-mono text-[11px] text-ink-200">{i + 1}</span>
          <span className="min-w-0 pt-0.5">{s}</span>
        </li>
      ))}
    </ol>
  );
}

/** "ntfy ✓ · app 1/2" for a log entry. */
function channelSummary(c: NotifyChannelResults | null | undefined): string {
  if (!c) return '';
  const parts: string[] = [];
  if (c.ntfy) parts.push(`ntfy ${c.ntfy.ok ? '✓' : '✗'}`);
  if (c.webpush) parts.push(`app ${c.webpush.sent}/${c.webpush.sent + c.webpush.failed}`);
  return parts.join(' · ');
}

const ROLE_LABEL: Record<string, string> = {
  owner: 'owner',
  writer: 'can make changes',
  reader: 'sees all event details',
  freeBusyReader: 'free/busy only',
};

function calendarName(c: GCalCalendar, ownId: string | null): string {
  if (c.summary) return c.summary;
  return c.id === ownId ? 'OmniFree' : c.id;
}

function GoogleCalendarCard({ copy }: { copy: (text: string, what: string) => void }) {
  const { client, notify } = useHub();
  const status = useHubData((c, s) => c.gcalStatus(s), []);
  const [syncing, setSyncing] = useState(false);
  const st = status.data;

  const syncNow = async () => {
    setSyncing(true);
    try {
      const r = await client.gcalSync();
      if (!r.configured) notify(r.skipped ?? 'Google Calendar is not set up', 'error');
      else
        notify(
          `Synced: ${r.upserted} updated, ${r.unchanged} unchanged, ${r.deleted} removed${r.failed ? `, ${r.failed} failed` : ''}${r.remaining ? ` (${r.remaining} left for the next run)` : ''}`,
          r.failed ? 'error' : 'ok',
        );
      status.reload();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setSyncing(false);
    }
  };

  const reads = st?.calendars.filter((c) => c.read && !c.write) ?? [];
  const sharedOk = reads.length > 0 && reads.every((c) => c.ok);
  const tone = !st ? 'off' : !st.configured ? 'warn' : st.calendars.every((c) => c.ok) ? 'ok' : 'warn';

  return (
    <div>
      <SectionTitle action={status.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh Google Calendar status" onClick={status.reload} />}>
        Google Calendar
      </SectionTitle>
      <Card className="space-y-3 p-3">
        <p className="flex items-center gap-2 text-sm text-ink-200">
          <StatusDot tone={tone} />
          {!st
            ? 'Checking…'
            : !st.configured
              ? 'Not configured'
              : !st.writable
                ? 'Reading only: the OmniFree calendar id is missing'
                : tone === 'ok'
                  ? 'Connected: tasks go to your OmniFree calendar instantly'
                  : 'Connected, but a calendar needs attention'}
        </p>
        {status.error && <ErrorNote message={status.error} onRetry={status.reload} />}
        {st && !st.configured && (
          <p className="text-xs leading-snug text-amber-400">
            Store the service-account key JSON in the vault key <span className="font-mono">{st.keys.sa_json}</span>, the OmniFree calendar id in{' '}
            <span className="font-mono">{st.keys.calendar_id}</span> and the calendars to read in <span className="font-mono">{st.keys.read_ids}</span>, then
            redeploy the hub. Until then everything here stays off and the feed below still works.
          </p>
        )}
        {st?.sa_email && (
          <div className="space-y-1">
            <p className="text-xs text-ink-400">Service account (share your calendar with this address)</p>
            <div className="flex items-center gap-1 rounded-row bg-ink-850 pl-3 ring-1 ring-inset ring-ink-700">
              <span className="min-w-0 flex-1 break-all py-2 font-mono text-[13px] text-ink-100">{st.sa_email}</span>
              <IconButton icon="copy" label="Copy the service account address" onClick={() => copy(st.sa_email ?? '', 'Address')} className="h-10 w-10" />
            </div>
          </div>
        )}
        {st && st.calendars.length > 0 && (
          <div className="divide-y divide-ink-800 rounded-row bg-ink-850 ring-1 ring-inset ring-ink-700">
            {st.calendars.map((c) => (
              <div key={c.id} className="flex items-start gap-2 px-3 py-2">
                <span className="pt-1.5">
                  <StatusDot tone={c.ok ? 'ok' : 'bad'} />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] text-ink-100">
                    {calendarName(c, st.calendar_id)} <span className="text-ink-500">· {c.write ? 'tasks are written here' : 'agenda read from here'}</span>
                  </p>
                  <p className="break-all text-[11px] text-ink-500">
                    {c.access_role ? (ROLE_LABEL[c.access_role] ?? c.access_role) : (c.error ?? 'no access')}
                    {c.access_role && !c.ok && <span className="text-amber-400"> · needs “{c.write ? 'Make changes to events' : 'See all event details'}”</span>}
                    {' · '}
                    <span className="font-mono">{c.id}</span>
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
        {st?.configured && (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" icon="refresh" onClick={() => void syncNow()} disabled={syncing || !st.writable}>
              {syncing ? 'Syncing…' : 'Sync now'}
            </Button>
            <span className="text-[12px] text-ink-500">
              {st.last_sync?.synced_at ? `Last full sync ${ago(st.last_sync.synced_at)}` : 'No full sync yet'}
              {st.last_push_at && <> · last push {ago(st.last_push_at)}</>}
            </span>
          </div>
        )}
        {st && st.errors.length > 0 && (
          <Help summary={`${st.errors.length} recent push error${st.errors.length === 1 ? '' : 's'}`}>
            <ul className="space-y-1 text-[12px] text-ink-400">
              {st.errors.map((e) => (
                <li key={e.task_id}>
                  <span className="text-danger-400">{e.error}</span> · {ago(e.synced_at)}
                </li>
              ))}
            </ul>
            <p className="mt-1 text-[11px] text-ink-500">Failed pushes are retried by Sync now and by the hourly schedule.</p>
          </Help>
        )}
        {(!sharedOk || !st?.configured) && (
          <Steps
            items={[
              <>Copy the service account address above.</>,
              <>On a computer, open Google Calendar (calendar.google.com). The phone app cannot share calendars.</>,
              <>
                Click the gear <b className="text-ink-100">⚙</b> at the top right, then <b className="text-ink-100">Settings</b>.
              </>,
              <>
                In the left column, under <b className="text-ink-100">Settings for my calendars</b>, click your main calendar (your name).
              </>,
              <>
                Scroll to <b className="text-ink-100">Share with specific people or groups</b> and click <b className="text-ink-100">+ Add people and groups</b>.
              </>,
              <>
                Paste the address, set <b className="text-ink-100">Permissions</b> to <b className="text-ink-100">See all event details</b> and click{' '}
                <b className="text-ink-100">Send</b>.
              </>,
              <>Come back here and tap the refresh icon: your calendar shows “sees all event details”.</>,
            ]}
          />
        )}
        <p className="text-[11px] leading-snug text-ink-500">
          Nothing to share for the <b className="text-ink-300">OmniFree</b> calendar: OmniFree owns it and has already shared it with you. It shows in Google
          Calendar under your calendars (if not, open the invitation e-mail and click <b className="text-ink-300">Add this calendar</b>). Tasks with a date
          appear in it within seconds, timed ones with a 10-minute reminder; done ones get a ✓. Your own events feed the morning push, the reminders, Home and
          Tasks.
        </p>
      </Card>
    </div>
  );
}

function CalendarCard({ copy }: { copy: (text: string, what: string) => void }) {
  const info = useHubData((c, s) => c.calendarInfo(s), []);
  return (
    <div>
      <SectionTitle>Other apps (Apple/Outlook) / fallback</SectionTitle>
      <Card className="space-y-3 p-3">
        <p className="flex items-center gap-2 text-sm text-ink-200">
          <Icon name="calendar" size={16} className="text-ink-400" />
          Your tasks as a subscribable calendar feed (ICS)
        </p>
        {info.loading && <Spinner />}
        {info.error && <ErrorNote message={info.error} onRetry={info.reload} />}
        {info.data && !info.data.configured && (
          <p className="text-xs text-amber-400">
            Not set up yet: store a long random value in the vault key <span className="font-mono">{info.data.token_key}</span> and redeploy the hub.
          </p>
        )}
        {info.data?.url && (
          <>
            <SecretRow label="Feed URL" value={info.data.url} masked={maskFeedUrl(info.data.url)} onCopy={() => copy(info.data?.url ?? '', 'Feed URL')} />
            <p className="text-[11px] text-ink-500">The URL is the password: anyone who has it can read your tasks. Do not share it.</p>
          </>
        )}
        <Steps
          items={[
            <>Copy the feed URL above.</>,
            <>On a computer, open Google Calendar (calendar.google.com). The phone app cannot add a URL.</>,
            <>In the left column, next to <b className="text-ink-100">Other calendars</b>, click <b className="text-ink-100">+</b> and choose <b className="text-ink-100">From URL</b>.</>,
            <>Paste the URL and click <b className="text-ink-100">Add calendar</b>. It then shows on your phone too.</>,
          ]}
        />
        <p className="text-[11px] leading-snug text-ink-500">
          With Google Calendar connected above you do not need this feed in Google. Google refreshes subscribed calendars on its own schedule, every few hours and up to about 24 h, so a new task can take a while to show. The same URL works in Apple Calendar (File &gt; New Calendar Subscription, or Settings &gt; Calendar &gt; Accounts on iPhone) and Outlook (Add calendar &gt; Subscribe from web). Done tasks of the last 14 days show with a ✓.
        </p>
      </Card>
    </div>
  );
}

export default function NotificationsView() {
  const { client, notify } = useHub();
  const { go } = useNav();
  const status = useHubData((c, s) => c.notifyStatus(s), []);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState<NotifyKind | null>(null);
  const [savingChannel, setSavingChannel] = useState<NotifyChannel | null>(null);
  const [ntfyTested, setNtfyTested] = useState(false);
  const [previewing, setPreviewing] = useState<NotifyKind | null>(null);
  const [preview, setPreview] = useState<NotifyResult | null>(null);

  const copy = async (text: string, what: string) => {
    const ok = await copyText(text);
    notify(ok ? `${what} copied` : 'Copy failed: select the text and copy it manually', ok ? 'ok' : 'error');
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const res = await client.notifyTest();
      const parts = [res.channels?.ntfy && (res.channels.ntfy.ok ? 'ntfy ✓' : 'ntfy failed'),
        res.channels?.webpush && `app ${res.channels.webpush.sent}/${res.channels.webpush.sent + res.channels.webpush.failed}`].filter(Boolean);
      notify(`Test push sent${parts.length ? ` (${parts.join(', ')})` : ''}: check your phone`, 'ok');
      setNtfyTested(true);
      status.reload();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setTesting(false);
    }
  };

  const toggle = async (kind: NotifyKind, enabled: boolean) => {
    setSaving(kind);
    status.setData((prev) => prev && { ...prev, prefs: { ...prev.prefs, [kind]: enabled } });
    try {
      const prefs = await client.setNotifyPrefs({ [kind]: enabled });
      status.setData((prev) => prev && { ...prev, prefs });
    } catch (err) {
      notify(errorText(err), 'error');
      status.reload();
    } finally {
      setSaving(null);
    }
  };

  const toggleChannel = async (channel: NotifyChannel, enabled: boolean) => {
    setSavingChannel(channel);
    status.setData((prev) => prev && { ...prev, channels: { ...prev.channels, [channel]: enabled } });
    try {
      const channels = await client.setNotifyChannels({ [channel]: enabled });
      status.setData((prev) => prev && { ...prev, channels });
    } catch (err) {
      notify(errorText(err), 'error');
      status.reload();
    } finally {
      setSavingChannel(null);
    }
  };

  const showPreview = async (kind: NotifyRunKind) => {
    setPreviewing(kind);
    try {
      setPreview(await client.notifyRun(kind, { preview: true }));
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setPreviewing(null);
    }
  };

  const st = status.data;
  const appLink = ntfySubscribeLink(st?.server, st?.topic);

  return (
    <div className="space-y-5">
      <AppPushCard
        channels={st?.channels ?? null}
        ntfyConfigured={st ? st.configured : null}
        onChannel={(c, on) => void toggleChannel(c, on)}
        savingChannel={savingChannel}
      />

      <div>
        <SectionTitle action={status.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh" onClick={status.reload} />}>Push notifications</SectionTitle>
        {status.error && <ErrorNote message={status.error} onRetry={status.reload} />}
        {!st && status.loading && <SkeletonList rows={2} />}
        {st && (
          <Card className="space-y-3 p-3">
            <p className="flex items-center gap-2 text-sm text-ink-200">
              <StatusDot tone={st.configured ? 'ok' : 'warn'} />
              {st.configured ? (st.channels.ntfy ? 'Ready: pushes go through ntfy' : 'Set up, but the ntfy channel is off') : 'Not set up yet'}
              <Badge className="ml-auto font-mono">{st.server.replace(/^https?:\/\//, '')}</Badge>
            </p>
            {st.configured && st.topic ? (
              <SecretRow label="Topic" value={st.topic} masked={maskSecret(st.topic)} onCopy={() => copy(st.topic ?? '', 'Topic')} />
            ) : (
              <p className="text-xs leading-snug text-amber-400">
                Store a long random topic name in the vault key <span className="font-mono">NTFY_TOPIC</span> and redeploy the hub. Anyone who knows the topic can read the pushes, so treat it like a password.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" icon="send" onClick={() => void sendTest()} disabled={!st.configured || testing}>
                {testing ? 'Sending...' : 'Send test'}
              </Button>
              {appLink && (
                <a href={appLink} className="press inline-flex min-h-[44px] items-center gap-2 rounded-full bg-ink-800 px-4 text-[15px] text-ink-100">
                  <Icon name="external" size={18} />
                  Open in ntfy
                </a>
              )}
            </div>
            {st.configured && (
              <Help summary="I didn’t receive it" open={ntfyTested}>
                <Checklist items={notReceivedChecks(currentBrowser(), 'ntfy')} />
              </Help>
            )}
          </Card>
        )}
      </div>

      <div>
        <SectionTitle>Subscribe on your phone</SectionTitle>
        <Card className="p-3">
          <Steps
            items={[
              <>
                Install the free <b className="text-ink-100">ntfy</b> app:{' '}
                <a className="text-signal underline" href={PLAY_URL} target="_blank" rel="noreferrer">Android</a> or{' '}
                <a className="text-signal underline" href={APPSTORE_URL} target="_blank" rel="noreferrer">iPhone</a>. No account needed.
              </>,
              <>Tap <b className="text-ink-100">+</b> (Subscribe to topic) and paste the topic copied above.{st?.server && st.server !== 'https://ntfy.sh' && <> Turn on “Use another server” and enter <span className="font-mono">{st.server}</span>.</>}</>,
              <>Allow notifications when asked, then tap <b className="text-ink-100">Send test</b> here.</>,
            ]}
          />
        </Card>
      </div>

      <div>
        <SectionTitle>What gets pushed</SectionTitle>
        <Card className="divide-y divide-ink-800">
          {NOTIFY_KINDS.map((k) => (
            <div key={k.kind} className="flex items-center gap-3 p-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm text-ink-100">{k.label}</p>
                <p className="text-xs leading-snug text-ink-400">{k.detail}</p>
              </div>
              {!EVENT_KINDS.includes(k.kind) &&
                (previewing === k.kind ? (
                  <Spinner className="m-3" />
                ) : (
                  <IconButton icon="eye" label={`Preview ${k.label}`} className="h-10 w-10" onClick={() => void showPreview(k.kind as NotifyRunKind)} />
                ))}
              <Toggle
                checked={st?.prefs[k.kind] ?? true}
                onChange={(v) => void toggle(k.kind, v)}
                label={k.label}
                disabled={!st || saving === k.kind}
              />
            </div>
          ))}
        </Card>
        {preview && (
          <Card className="mt-2 space-y-1 p-3">
            <p className="flex items-center gap-2 text-xs text-ink-400">
              <Icon name="bell" size={14} /> Preview ({preview.kind}){preview.skipped && preview.skipped !== 'preview' && `: ${preview.skipped}`}
              <IconButton icon="x" label="Close preview" className="ml-auto h-8 w-8" onClick={() => setPreview(null)} />
            </p>
            {preview.title && <p className="text-sm font-semibold text-ink-100">{preview.title}</p>}
            {preview.message && <p className="whitespace-pre-line text-[13px] text-ink-300">{preview.message}</p>}
          </Card>
        )}
        <button
          type="button"
          onClick={() => go('more', 'schedules')}
          className="press mt-2 flex min-h-[44px] w-full items-center gap-2 rounded-row px-1 text-left text-[13px] text-ink-300"
        >
          <Icon name="clock" size={16} className="text-ink-500" />
          <span className="min-w-0 flex-1">Pushes are sent by schedules: add them from Schedules &gt; New schedule &gt; templates.</span>
          <Icon name="right" size={16} className="text-ink-500" />
        </button>
      </div>

      {st && st.recent.length > 0 && (
        <div>
          <SectionTitle>Recent pushes</SectionTitle>
          <Card className="divide-y divide-ink-800">
            {st.recent.slice(0, 10).map((r, i) => (
              <div key={`${r.sent_at}-${i}`} className="flex items-start gap-2 px-3 py-2">
                <span className="pt-1.5">
                  <StatusDot tone={r.status === 'sent' ? 'ok' : r.status === 'failed' ? 'bad' : 'warn'} />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] text-ink-100">{r.title || r.kind}</p>
                  <p className="text-[11px] text-ink-500">
                    {r.kind} · {ago(r.sent_at)}
                    {channelSummary(r.channels) && <> · {channelSummary(r.channels)}</>}
                    {r.error && <span className="text-danger-400"> · {r.error}</span>}
                  </p>
                </div>
              </div>
            ))}
          </Card>
        </div>
      )}

      <GoogleCalendarCard copy={(t, w) => void copy(t, w)} />

      <CalendarCard copy={(t, w) => void copy(t, w)} />
    </div>
  );
}
