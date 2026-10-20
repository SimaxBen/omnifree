import { useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { Icon } from '../components/icons';
import { Badge, EmptyState, ErrorNote, IconButton, SectionTitle, Spinner, StatusDot, Toggle, cx } from '../components/ui';
import { formatDateTime, formatMs } from '../lib/format';
import { errorText, useHub, useHubData, useHubEvents } from '../lib/hub';
import { moveItem } from '../lib/list';
import { readPref, writePref } from '../lib/settings';
import type { ProviderStatus } from '../lib/types';

const KINDS = ['llm', 'storage', 'db', 'compute', 'dns', 'cron'] as const;
type Kind = (typeof KINDS)[number];

function tone(p: ProviderStatus): 'ok' | 'warn' | 'bad' | 'off' {
  if (!p.enabled) return 'off';
  if (p.load_error || p.breaker === 'open') return 'bad';
  if (p.missing_secrets.length || p.breaker === 'half_open' || (p.exhausted_until && new Date(p.exhausted_until) > new Date())) return 'warn';
  if (p.last_health && !p.last_health.ok) return 'bad';
  return 'ok';
}

export default function SwitchboardView() {
  const { client, notify } = useHub();
  const [kind, setKind] = useState<Kind>(() => readPref<Kind>('plugins.kind', 'llm'));
  const plugins = useHubData((c, s) => c.plugins(undefined, s), [], 30_000);
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [drag, setDrag] = useState<{ name: string; over: number } | null>(null);
  const rowRefs = useRef(new Map<string, HTMLLIElement>());
  const timer = useRef<number | undefined>(undefined);

  useHubEvents(['registry.*', 'breaker.*', 'health.*', 'quota.exhausted'], () => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(plugins.reload, 800);
  });

  const chooseKind = (k: Kind) => {
    setKind(k);
    writePref('plugins.kind', k);
  };

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of plugins.data ?? []) c[p.kind] = (c[p.kind] ?? 0) + 1;
    return c;
  }, [plugins.data]);

  const rows = useMemo(
    () => (plugins.data ?? []).filter((p) => p.kind === kind).sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name)),
    [plugins.data, kind],
  );

  const withPending = async (name: string, fn: () => Promise<void>) => {
    setPending((s) => new Set(s).add(name));
    try {
      await fn();
    } catch (err) {
      notify(`${name}: ${errorText(err)}`, 'error');
      plugins.reload();
    } finally {
      setPending((s) => {
        const n = new Set(s);
        n.delete(name);
        return n;
      });
    }
  };

  const setEnabled = (p: ProviderStatus, enabled: boolean) =>
    withPending(p.name, async () => {
      plugins.setData((prev) => prev?.map((x) => (x.name === p.name ? { ...x, enabled } : x)));
      await client.setPluginEnabled(p.name, enabled);
    });

  const commitOrder = async (order: string[]) => {
    const priorities = new Map(order.map((n, i) => [n, (i + 1) * 10]));
    plugins.setData((prev) => prev?.map((x) => (priorities.has(x.name) ? { ...x, priority: priorities.get(x.name)! } : x)));
    try {
      await client.reorderPlugins(kind, order);
    } catch (err) {
      notify(`Reorder failed: ${errorText(err)}`, 'error');
      plugins.reload();
    }
  };

  const move = (index: number, delta: number) => {
    const to = index + delta;
    if (to < 0 || to >= rows.length) return;
    void commitOrder(moveItem(rows, index, to).map((r) => r.name));
  };

  const health = (p: ProviderStatus) =>
    withPending(p.name, async () => {
      const h = await client.checkPluginHealth(p.name);
      plugins.setData((prev) => prev?.map((x) => (x.name === p.name ? { ...x, last_health: h } : x)));
      notify(`${p.name}: ${h.ok ? 'healthy' : 'unhealthy'}${h.latency_ms != null ? ` (${formatMs(h.latency_ms)})` : ''}${h.detail && !h.ok ? ` - ${h.detail}` : ''}`, h.ok ? 'ok' : 'error');
    });

  const reset = (p: ProviderStatus) =>
    withPending(p.name, async () => {
      await client.resetPlugin(p.name);
      plugins.reload();
    });

  // Pointer-based drag (works with touch, unlike HTML5 drag and drop).
  const overIndexAt = (clientY: number): number => {
    let idx = rows.length - 1;
    for (let i = 0; i < rows.length; i++) {
      const el = rowRefs.current.get(rows[i].name);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (clientY < r.top + r.height / 2) {
        idx = i;
        break;
      }
    }
    return idx;
  };

  const onGripDown = (e: ReactPointerEvent<HTMLButtonElement>, name: string, index: number) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    setDrag({ name, over: index });
  };
  const onGripMove = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!drag) return;
    const over = overIndexAt(e.clientY);
    if (over !== drag.over) setDrag({ ...drag, over });
  };
  const onGripUp = () => {
    if (!drag) return;
    const from = rows.findIndex((r) => r.name === drag.name);
    const to = drag.over;
    setDrag(null);
    if (from >= 0 && from !== to) void commitOrder(moveItem(rows, from, to).map((r) => r.name));
  };

  const displayRows = drag ? moveItem(rows, rows.findIndex((r) => r.name === drag.name), drag.over) : rows;

  return (
    <div className="space-y-4">
      <div className="scroll-thin -mx-4 flex gap-1.5 overflow-x-auto px-4">
        {KINDS.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => chooseKind(k)}
            className={cx(
              'min-h-[36px] shrink-0 rounded-full border px-3 font-mono text-xs transition-colors',
              k === kind ? 'border-signal/50 bg-signal/10 text-signal' : 'border-ink-700 text-ink-400 hover:text-ink-200',
            )}
          >
            {k}
            {counts[k] ? <span className="ml-1 text-ink-500">{counts[k]}</span> : null}
          </button>
        ))}
      </div>

      <SectionTitle action={plugins.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh plugins" onClick={plugins.reload} />}>
        Fallback chain
      </SectionTitle>
      {plugins.error && <ErrorNote message={plugins.error} onRetry={plugins.reload} />}
      {!plugins.loading && !plugins.error && rows.length === 0 && <EmptyState icon="switchboard" title={`No ${kind} plugins`} />}

      <ol className="space-y-2">
        {displayRows.map((p) => {
          const index = rows.findIndex((r) => r.name === p.name);
          const busy = pending.has(p.name);
          const exhausted = p.exhausted_until && new Date(p.exhausted_until) > new Date();
          const latency = p.last_health?.latency_ms ?? p.avg_latency_ms ?? p.last_latency_ms;
          return (
            <li
              key={p.name}
              ref={(el) => {
                if (el) rowRefs.current.set(p.name, el);
                else rowRefs.current.delete(p.name);
              }}
              className={cx(
                'rounded-xl border bg-ink-900/90 transition-shadow',
                drag?.name === p.name ? 'border-signal/60 shadow-lg shadow-signal/10' : 'border-ink-700/70',
                !p.enabled && 'opacity-60',
              )}
            >
              <div className="flex items-center gap-2 p-2 pr-3">
                <button
                  type="button"
                  aria-label={`Drag ${p.name} to reorder`}
                  className="flex h-10 w-7 shrink-0 cursor-grab touch-none items-center justify-center text-ink-500 active:cursor-grabbing"
                  onPointerDown={(e) => onGripDown(e, p.name, index)}
                  onPointerMove={onGripMove}
                  onPointerUp={onGripUp}
                  onPointerCancel={() => setDrag(null)}
                >
                  <Icon name="grip" size={18} strokeWidth={3} />
                </button>
                <span className="w-5 shrink-0 text-center font-mono text-[11px] text-ink-500">{index + 1}</span>
                <StatusDot tone={tone(p)} />
                <div className="min-w-0 flex-1">
                  <p className="truncate font-mono text-sm text-ink-100">{p.display_name ?? p.name}</p>
                  <p className="truncate text-[11px] text-ink-500">
                    {p.group} · {formatMs(latency)} · {p.successes} ok / {p.failures} fail
                  </p>
                </div>
                {busy ? <Spinner /> : <Toggle checked={p.enabled} onChange={(v) => void setEnabled(p, v)} label={`Enable ${p.name}`} />}
              </div>

              <div className="flex flex-wrap items-center gap-1.5 border-t border-ink-800 px-3 py-1.5">
                {p.breaker !== 'closed' && <Badge tone={p.breaker === 'open' ? 'bad' : 'warn'}>breaker {p.breaker}</Badge>}
                {exhausted && <Badge tone="warn">quota until {formatDateTime(p.exhausted_until)}</Badge>}
                {p.missing_secrets.length > 0 && <Badge tone="warn">missing {p.missing_secrets.join(', ')}</Badge>}
                {p.load_error && <Badge tone="bad">load error</Badge>}
                {p.last_health && (
                  <Badge tone={p.last_health.ok ? 'ok' : 'bad'}>health {p.last_health.ok ? 'ok' : 'fail'}</Badge>
                )}
                <span className="ml-auto flex items-center">
                  <IconButton icon="up" label={`Move ${p.name} up`} disabled={index === 0} onClick={() => move(index, -1)} className="h-9 w-9" />
                  <IconButton icon="down" label={`Move ${p.name} down`} disabled={index === rows.length - 1} onClick={() => move(index, 1)} className="h-9 w-9" />
                  <IconButton icon="pulse" label={`Check ${p.name} health`} disabled={busy} onClick={() => void health(p)} className="h-9 w-9" />
                  {(p.breaker !== 'closed' || exhausted) && (
                    <IconButton icon="refresh" label={`Reset ${p.name}`} disabled={busy} onClick={() => void reset(p)} className="h-9 w-9" />
                  )}
                </span>
              </div>
              {(p.load_error || p.last_error) && (
                <p className="break-words border-t border-ink-800 px-3 py-1.5 font-mono text-[11px] text-danger-400/90">{p.load_error ?? p.last_error}</p>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
