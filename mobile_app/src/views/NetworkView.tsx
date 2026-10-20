import { useState } from 'react';
import { Badge, Card, EmptyState, ErrorNote, IconButton, SectionTitle, Spinner, StatusDot, Toggle, cx } from '../components/ui';
import { formatNumber, formatTime } from '../lib/format';
import { errorText, useHub, useHubData, useHubEvents } from '../lib/hub';
import type { VMInstance } from '../lib/types';

const PAUSES: { label: string; seconds: number | undefined }[] = [
  { label: '5 min', seconds: 300 },
  { label: '30 min', seconds: 1800 },
  { label: '1 hour', seconds: 3600 },
  { label: 'Until re-enabled', seconds: undefined },
];

function nodeTone(n: VMInstance): 'ok' | 'warn' | 'bad' | 'off' {
  if (n.state === 'online' || n.state === 'running') return 'ok';
  if (n.state === 'offline' || n.state === 'stopped') return 'off';
  if (n.state === 'error') return 'bad';
  return 'warn';
}

export default function NetworkView() {
  const { client, notify } = useHub();
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [askPause, setAskPause] = useState(false);
  const [switching, setSwitching] = useState(false);
  const status = useHubData((c, s) => c.dnsStatus(s), [], 30_000);
  const log = useHubData((c, s) => c.dnsQueryLog(100, blockedOnly, s), [blockedOnly], 30_000);
  const nodes = useHubData((c, s) => c.tailscaleNodes(s), [], 60_000);

  useHubEvents(['dns.*'], () => status.reload());
  useHubEvents(['compute.state_changed'], (e) => {
    if (e.provider === 'tailscale') nodes.reload();
  });

  const setBlocking = async (enabled: boolean, seconds?: number) => {
    setSwitching(true);
    setAskPause(false);
    try {
      const next = await client.dnsSetBlocking(enabled, seconds);
      status.setData(() => next);
      notify(enabled ? 'Ad blocking enabled' : seconds ? `Blocking paused for ${Math.round(seconds / 60)} min` : 'Ad blocking disabled', 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setSwitching(false);
    }
  };

  const s = status.data;
  const nodeList = [...(nodes.data ?? [])].sort((a, b) => Number(nodeTone(b) === 'ok') - Number(nodeTone(a) === 'ok') || a.name.localeCompare(b.name));

  return (
    <div className="space-y-5">
      <div>
        <SectionTitle action={status.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh DNS status" onClick={status.reload} />}>Pi-hole</SectionTitle>
        {status.error && <ErrorNote message={status.error} onRetry={status.reload} />}
        {s && (
          <Card className="p-3">
            <div className="flex items-center gap-3">
              <span className={cx('rounded-lg p-2', s.blocking_enabled ? 'bg-signal/10 text-signal' : 'bg-ink-800 text-ink-400')}>
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                  <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" />
                </svg>
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink-100">{s.blocking_enabled ? 'Blocking ads' : 'Blocking off'}</p>
                <p className="text-xs text-ink-400">
                  {s.blocking_resumes_at ? `resumes at ${formatTime(s.blocking_resumes_at)}` : s.provider || 'pihole'}
                </p>
              </div>
              {switching ? (
                <Spinner />
              ) : (
                <Toggle
                  checked={s.blocking_enabled}
                  label="Ad blocking"
                  onChange={(v) => (v ? void setBlocking(true) : setAskPause(true))}
                />
              )}
            </div>
            {askPause && (
              <div className="mt-3 space-y-2 border-t border-ink-800 pt-3">
                <p className="text-xs text-ink-400">Disable blocking for:</p>
                <div className="grid grid-cols-2 gap-2">
                  {PAUSES.map((p) => (
                    <button
                      key={p.label}
                      type="button"
                      onClick={() => void setBlocking(false, p.seconds)}
                      className="min-h-[40px] rounded-lg border border-ink-700 bg-ink-850 text-sm text-ink-100 hover:border-amber-400/60"
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                <button type="button" onClick={() => setAskPause(false)} className="w-full py-1 text-xs text-ink-400">
                  Keep blocking
                </button>
              </div>
            )}
            <dl className="mt-3 grid grid-cols-3 gap-2 border-t border-ink-800 pt-3 text-center">
              <div>
                <dt className="text-[11px] text-ink-500">Queries</dt>
                <dd className="font-mono text-sm text-ink-100">{formatNumber(s.queries_today)}</dd>
              </div>
              <div>
                <dt className="text-[11px] text-ink-500">Blocked</dt>
                <dd className="font-mono text-sm text-ink-100">{formatNumber(s.blocked_today)}</dd>
              </div>
              <div>
                <dt className="text-[11px] text-ink-500">Rate</dt>
                <dd className="font-mono text-sm text-ink-100">{s.percent_blocked == null ? '-' : `${s.percent_blocked.toFixed(1)}%`}</dd>
              </div>
            </dl>
            {s.domains_on_blocklist != null && (
              <p className="mt-2 text-center text-[11px] text-ink-500">{formatNumber(s.domains_on_blocklist)} domains on blocklist</p>
            )}
          </Card>
        )}
      </div>

      <div>
        <SectionTitle
          action={
            <label className="flex items-center gap-2 text-xs text-ink-400">
              Blocked only
              <Toggle checked={blockedOnly} onChange={setBlockedOnly} label="Blocked only" />
            </label>
          }
        >
          Query log
        </SectionTitle>
        {log.error && <ErrorNote message={log.error} onRetry={log.reload} />}
        {log.loading && !log.data && <Spinner />}
        {log.data && log.data.length === 0 && <EmptyState icon="shield" title="No queries in the log" />}
        {log.data && log.data.length > 0 && (
          <ul className="scroll-thin max-h-80 divide-y divide-ink-800 overflow-y-auto rounded-xl border border-ink-700/70 bg-ink-900/80">
            {log.data.map((q, i) => (
              <li key={`${q.timestamp}-${q.domain}-${i}`} className="flex items-center gap-2 px-3 py-1.5">
                <StatusDot tone={q.blocked ? 'bad' : 'ok'} />
                <span className="min-w-0 flex-1">
                  <span className={cx('block truncate font-mono text-[12px]', q.blocked ? 'text-danger-400' : 'text-ink-200')}>{q.domain}</span>
                  <span className="text-[11px] text-ink-500">
                    {formatTime(q.timestamp)}
                    {q.client && ` · ${q.client}`}
                    {q.query_type && ` · ${q.query_type}`}
                  </span>
                </span>
                {q.status && <Badge tone={q.blocked ? 'bad' : 'neutral'}>{q.status}</Badge>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <SectionTitle action={nodes.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh Tailscale nodes" onClick={nodes.reload} />}>
          Tailscale mesh
        </SectionTitle>
        {nodes.error && <ErrorNote message={nodes.error} onRetry={nodes.reload} />}
        {!nodes.loading && !nodes.error && nodeList.length === 0 && <EmptyState icon="network" title="No Tailscale nodes" />}
        <div className="grid gap-2 sm:grid-cols-2">
          {nodeList.map((n) => (
            <Card key={n.id} className="flex items-center gap-3 p-3">
              <StatusDot tone={nodeTone(n)} pulse={nodeTone(n) === 'ok'} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-ink-100">{n.name}</p>
                <p className="truncate font-mono text-[11px] text-ink-400">
                  {n.tailscale_ip ?? n.private_ip ?? n.public_ip ?? 'no address'}
                  {typeof n.meta?.os === 'string' && ` · ${n.meta.os}`}
                </p>
              </div>
              <Badge tone={nodeTone(n) === 'ok' ? 'ok' : 'neutral'}>{n.state}</Badge>
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}
