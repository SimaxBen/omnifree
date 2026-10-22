// Offline mode, the coordinator: connectivity state for the banner, the queue counters, and when to
// replay (back online, app start, every SYNC_EVERY_MS while writes wait, and right after a write).

import type { QueueItem } from './model';
import { discardItem, enqueue, replay, retryItem, type ReplayResult, type Send } from './queue';
import { IdbStore, type OfflineStore } from './store';

export interface OfflineState {
  /** False when the browser says offline or the hub could not be reached. */
  online: boolean;
  /** When the oldest saved response on screen was fetched (null: everything is live). */
  cachedAt: number | null;
  pending: number;
  conflicts: number;
  syncing: boolean;
  lastSync: number | null;
  /** The hub refused the token during the last replay (401/403): sync is paused, not offline. */
  authError: boolean;
}

export const SYNC_EVERY_MS = 30_000;

type Listener = (s: OfflineState) => void;

function browserOnline(): boolean {
  return typeof navigator === 'undefined' || navigator.onLine !== false;
}

export class OfflineManager {
  state: OfflineState = { online: true, cachedAt: null, pending: 0, conflicts: 0, syncing: false, lastSync: null, authError: false };
  private listeners = new Set<Listener>();
  private refreshers = new Set<() => void>();
  private send: Send | null = null;
  private running: Promise<ReplayResult | null> | null = null;
  private reachable = true;
  private timer: ReturnType<typeof setInterval> | undefined;
  /** The hub URL in the settings: writes queued for it, saved data from it. */
  private hub = '';

  constructor(
    readonly store: OfflineStore,
    private readonly isBrowserOnline: () => boolean = browserOnline,
    readonly now: () => number = () => Date.now(),
  ) {}

  /** The hub to replay against (the current HubClient's address and token). */
  setSender(send: Send): void {
    this.send = send;
  }

  /**
   * The settings changed: writes are only replayed to the hub they were made for, and saved data
   * from another hub is dropped (all of it when the token was removed). Queued writes for another
   * hub stay dormant until that hub is configured again.
   */
  async bindHub(hub: string, hasToken: boolean): Promise<void> {
    this.hub = hub;
    const entries = await this.store.cacheAll().catch(() => []);
    for (const e of entries) {
      if (!hasToken || (e.hub && e.hub !== hub)) await this.store.cacheDelete(e.key).catch(() => undefined);
    }
    await this.counts().catch(() => undefined);
  }

  private mine(i: QueueItem): boolean {
    return !i.hub || i.hub === this.hub;
  }

  isOnline(): boolean {
    return this.isBrowserOnline() && this.reachable;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => {
      this.listeners.delete(fn);
    };
  }

  /** Called when screens should reload (writes synced, or live data is back). */
  onRefresh(fn: () => void): () => void {
    this.refreshers.add(fn);
    return () => {
      this.refreshers.delete(fn);
    };
  }

  private set(patch: Partial<OfflineState>): void {
    this.state = { ...this.state, ...patch };
    for (const l of [...this.listeners]) l(this.state);
  }

  private refresh(): void {
    for (const r of [...this.refreshers]) r();
  }

  /** The queue entries of the current hub. */
  async items(): Promise<QueueItem[]> {
    return (await this.store.queueAll()).filter((i) => this.mine(i));
  }

  async pendingItems(): Promise<QueueItem[]> {
    return (await this.items()).filter((i) => i.status === 'pending');
  }

  async counts(): Promise<void> {
    const all = await this.items();
    this.set({ pending: all.filter((i) => i.status === 'pending').length, conflicts: all.filter((i) => i.status === 'conflict').length });
  }

  async enqueue(item: QueueItem): Promise<void> {
    await enqueue(this.store, { ...item, hub: item.hub ?? this.hub });
    await this.counts();
    void this.sync();
  }

  /** A saved response was shown because the hub was unreachable. */
  servedFromCache(savedAt: number): void {
    this.reachable = false; // writes queue until a request or a replay gets through again
    this.set({ online: false, cachedAt: this.state.cachedAt === null ? savedAt : Math.min(this.state.cachedAt, savedAt) });
  }

  /** A request could not reach the hub. */
  unreachable(): void {
    this.reachable = false;
    if (this.state.online) this.set({ online: false });
  }

  /** A live response arrived: leave offline mode, reload what showed saved data, replay the queue. */
  reached(): void {
    this.reachable = true;
    if (this.state.online && this.state.cachedAt === null) {
      if (this.state.authError) {
        this.set({ authError: false }); // the token works again: retry the paused queue
        void this.sync();
      }
      return;
    }
    this.set({ online: true, cachedAt: null });
    this.refresh();
    void this.sync();
  }

  /**
   * Replays the queue (a call during a run waits for it, then runs again for what was queued meanwhile).
   * ``force`` also retries entries waiting out a server-error backoff.
   */
  sync(force = false): Promise<ReplayResult | null> {
    if (this.running) return this.running.then(() => this.sync(force));
    // Started on the next microtask, so `running` is set before the run can clear it.
    this.running = Promise.resolve().then(async () => {
      let total: ReplayResult | null = null;
      try {
        if (this.send && this.isBrowserOnline() && (await this.pendingItems()).length) {
          this.set({ syncing: true });
          total = await replay(this.store, this.send, { now: this.now(), force, hub: this.hub });
          // Only "no answer at all" means offline; a 5xx or 401 came from a reachable hub.
          this.reachable = !total.unreachable;
        }
      } finally {
        this.running = null;
        await this.counts().catch(() => undefined);
        const patch: Partial<OfflineState> = { syncing: false };
        const wasCached = this.state.cachedAt !== null;
        if (total && !total.unreachable) {
          if (!total.authError) patch.lastSync = this.now();
          patch.authError = total.authError;
          patch.online = this.isBrowserOnline();
          patch.cachedAt = null; // the hub answered: screens reload live data below
        } else if (total?.unreachable) patch.online = false;
        this.set(patch);
        if (total && (total.synced > 0 || total.conflicts > 0 || (wasCached && !total.unreachable))) this.refresh();
      }
      return total;
    });
    return this.running;
  }

  async retry(seq: number): Promise<void> {
    await retryItem(this.store, seq);
    await this.counts();
    void this.sync();
  }

  async discard(seq: number): Promise<void> {
    await discardItem(this.store, seq);
    await this.counts();
    this.refresh();
  }

  /** Wires the browser events and the periodic replay; returns the cleanup. */
  start(win: Pick<Window, 'addEventListener' | 'removeEventListener'> | undefined = typeof window === 'undefined' ? undefined : window): () => void {
    const onOnline = () => {
      this.reachable = true;
      this.set({ online: true });
      void this.sync().then(() => this.refresh());
    };
    const onOffline = () => this.set({ online: false });
    win?.addEventListener('online', onOnline);
    win?.addEventListener('offline', onOffline);
    if (!this.isBrowserOnline()) this.set({ online: false });
    void this.counts().then(() => this.sync());
    this.timer = setInterval(() => {
      if (this.state.pending > 0) void this.sync();
    }, SYNC_EVERY_MS);
    return () => {
      win?.removeEventListener('online', onOnline);
      win?.removeEventListener('offline', onOffline);
      clearInterval(this.timer);
    };
  }
}

/** The app-wide instance (one IndexedDB database per browser profile). */
export const offline = new OfflineManager(new IdbStore());
