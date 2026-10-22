import { useCallback, useEffect, useState } from 'react';
import { offline, type OfflineState } from '../lib/offline/manager';
import { bannerText, clockOf, isPending, type QueueItem } from '../lib/offline/model';
import { Icon } from './icons';
import { Sheet } from './Sheet';
import { Badge, Button, cx, haptic } from './ui';

/** Live offline state (connectivity, saved data on screen, queue counters). */
export function useOffline(): OfflineState {
  const [state, setState] = useState<OfflineState>(offline.state);
  useEffect(() => {
    const off = offline.subscribe(setState);
    return off;
  }, []);
  return state;
}

/** "pending sync" pill for rows created or changed offline. */
export function PendingBadge({ item, className }: { item: unknown; className?: string }) {
  if (!isPending(item)) return null;
  return (
    <span className={cx('inline-flex shrink-0 items-center gap-1 rounded-full bg-amber-400/15 px-2 py-0.5 text-[11px] leading-4 text-amber-400', className)}>
      <Icon name="refresh" size={11} />
      pending sync
    </span>
  );
}

function SyncSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const state = useOffline();
  const [items, setItems] = useState<QueueItem[]>([]);
  const load = useCallback(() => void offline.items().then(setItems, () => setItems([])), []);
  useEffect(() => {
    if (open) load();
  }, [open, load, state.pending, state.conflicts, state.syncing]);

  const conflicts = items.filter((i) => i.status === 'conflict');
  const pending = items.filter((i) => i.status === 'pending');
  const act = async (fn: () => Promise<void>) => {
    haptic(8);
    await fn();
    load();
  };

  const row = (i: QueueItem) => (
    <li key={i.seq} className="flex items-start gap-3 py-3">
      <Icon name={i.status === 'conflict' ? 'x' : i.kind === 'voice' ? 'mic' : 'refresh'} size={18} className={cx('mt-0.5 shrink-0', i.status === 'conflict' ? 'text-danger-400' : 'text-amber-400')} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[15px] text-ink-100">{i.label}</span>
        <span className="block text-[12px] text-ink-500">
          {clockOf(i.createdAt)}
          {i.status === 'conflict' && i.error ? ` · ${i.httpStatus ? `${i.httpStatus}: ` : ''}${i.error}` : ''}
          {i.status === 'pending' && i.attempts > 0 ? ` · ${i.attempts} attempt${i.attempts === 1 ? '' : 's'}${i.error ? `, ${i.error}` : ''}` : ''}
        </span>
      </span>
      <span className="flex shrink-0 gap-1">
        {i.status === 'conflict' && (
          <Button variant="subtle" className="min-h-[36px] px-3 text-[13px]" onClick={() => void act(() => offline.retry(i.seq as number))}>
            Retry
          </Button>
        )}
        <Button variant="ghost" className="min-h-[36px] px-3 text-[13px]" onClick={() => void act(() => offline.discard(i.seq as number))}>
          Discard
        </Button>
      </span>
    </li>
  );

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Offline & sync"
      footer={
        <Button variant="primary" icon="refresh" className="w-full" disabled={state.syncing || !pending.length} onClick={() => void act(async () => void (await offline.sync()))}>
          {state.syncing ? 'Syncing…' : 'Sync now'}
        </Button>
      }
    >
      <div className="space-y-5">
        <p className="text-[14px] text-ink-400">
          {state.cachedAt !== null
            ? `The hub is out of reach. Lists show what was saved at ${clockOf(state.cachedAt)}.`
            : state.online
              ? state.lastSync
                ? `Online. Last sync at ${clockOf(state.lastSync)}.`
                : 'Online.'
              : 'Offline. New tasks, notes, expenses, incomes and voice recordings are kept on this phone.'}{' '}
          Changes made offline are sent in order as soon as the hub answers.
        </p>
        {conflicts.length > 0 && (
          <section>
            <h3 className="flex items-center gap-2 pb-1 font-display text-[15px] font-semibold text-ink-200">
              Conflicts <Badge tone="bad">{conflicts.length}</Badge>
            </h3>
            <p className="pb-1 text-[13px] text-ink-500">The hub refused these changes (the item may have been deleted or changed elsewhere). Retry, or discard them.</p>
            <ul className="divide-y divide-ink-800">{conflicts.map(row)}</ul>
          </section>
        )}
        <section>
          <h3 className="flex items-center gap-2 pb-1 font-display text-[15px] font-semibold text-ink-200">
            Waiting to sync <Badge tone={pending.length ? 'warn' : 'neutral'}>{pending.length}</Badge>
          </h3>
          {pending.length ? <ul className="divide-y divide-ink-800">{pending.map(row)}</ul> : <p className="text-[13px] text-ink-500">Nothing waiting.</p>}
        </section>
      </div>
    </Sheet>
  );
}

/** The strip under the header: offline / saved data / pending sync / conflicts; tap for the sync sheet. */
export function OfflineBanner() {
  const state = useOffline();
  const [open, setOpen] = useState(false);
  const banner = bannerText(state);
  return (
    <>
      {banner && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className={cx(
            'screen-in-up mx-4 mb-2 flex min-h-[40px] w-[calc(100%-2rem)] items-center gap-2 rounded-row px-4 py-2 text-left text-[14px]',
            banner.tone === 'conflict' && 'bg-danger-400/15 text-danger-400',
            banner.tone === 'offline' && 'bg-ink-850 text-ink-200 ring-1 ring-inset ring-ink-700',
            banner.tone === 'sync' && 'bg-amber-400/15 text-amber-400',
          )}
          aria-label={`${banner.text}. Open sync details`}
        >
          <Icon name={banner.tone === 'conflict' ? 'x' : banner.tone === 'offline' ? 'wave' : 'refresh'} size={16} className={cx('shrink-0', state.syncing && 'spin')} />
          <span className="min-w-0 flex-1">{banner.text}</span>
          <Icon name="right" size={14} className="shrink-0 opacity-70" />
        </button>
      )}
      <SyncSheet open={open} onClose={() => setOpen(false)} />
    </>
  );
}
