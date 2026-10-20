// One shared WebSocket to hub_api /ws with exponential-backoff reconnect and
// pattern-based listeners ("router.*", "instinct.task.screenshot", "*").
// When the handshake keeps failing (serverless hubs cannot upgrade), it falls back to
// polling GET /events/recent and reports the 'polling' state.

import type { HubEvent } from './types';

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'polling' | 'closed';

export interface EventStreamOptions {
  /** Fetches recent events (oldest first); enables the polling fallback. */
  poll?: (signal: AbortSignal) => Promise<HubEvent[]>;
  /** Polling period in ms (default 10 s). */
  pollMs?: number;
  /** Consecutive failed handshakes before falling back to polling (default 3). */
  maxFailures?: number;
}

type Listener = (event: HubEvent) => void;

export function matchesPattern(type: string, pattern: string): boolean {
  if (pattern === '*') return true;
  if (pattern.endsWith('.*')) return type === pattern.slice(0, -2) || type.startsWith(pattern.slice(0, -1));
  return type === pattern;
}

/** Event families the PWA consumes; the hub may filter on them (see API_ASSUMPTIONS.md). */
export const SUBSCRIBED_TYPES = ['router.*', 'quota.*', 'breaker.*', 'health.*', 'registry.*', 'storage.*', 'instinct.*', 'dns.*', 'compute.*'];

export class EventStream {
  private ws: WebSocket | null = null;
  private listeners = new Set<{ patterns: string[]; fn: Listener }>();
  private stateListeners = new Set<(s: ConnectionState) => void>();
  private retry = 0;
  private timer: number | undefined;
  private stopped = true;
  private _state: ConnectionState = 'idle';
  private recentEvents: HubEvent[] = [];
  private failures = 0;
  private pollTimer: number | undefined;
  private pollCtrl: AbortController | null = null;
  private seen = new Set<string>();
  private seeded = false;

  constructor(
    private readonly url: string,
    private readonly protocols?: string[],
    private readonly options: EventStreamOptions = {},
  ) {}

  get state(): ConnectionState {
    return this._state;
  }

  get recent(): HubEvent[] {
    return this.recentEvents;
  }

  start(): void {
    this.stopped = false;
    this.failures = 0;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    window.clearTimeout(this.timer);
    this.stopPolling();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this.setState('closed');
  }

  on(patterns: string[], fn: Listener): () => void {
    const entry = { patterns, fn };
    this.listeners.add(entry);
    return () => {
      this.listeners.delete(entry);
    };
  }

  onState(fn: (s: ConnectionState) => void): () => void {
    this.stateListeners.add(fn);
    return () => {
      this.stateListeners.delete(fn);
    };
  }

  private setState(s: ConnectionState) {
    this._state = s;
    this.stateListeners.forEach((fn) => fn(s));
  }

  /** Records and dispatches one event; duplicates (same id) are dropped. */
  private deliver(event: HubEvent) {
    if (event.id) {
      if (this.seen.has(event.id)) return;
      this.seen.add(event.id);
      if (this.seen.size > 1000) this.seen = new Set([...this.seen].slice(-500));
    }
    this.recentEvents = [event, ...this.recentEvents].slice(0, 200);
    for (const l of this.listeners) {
      if (l.patterns.some((p) => matchesPattern(event.type, p))) l.fn(event);
    }
  }

  private connect() {
    if (this.stopped) return;
    this.setState('connecting');
    let ws: WebSocket;
    try {
      ws = this.protocols ? new WebSocket(this.url, this.protocols) : new WebSocket(this.url);
    } catch {
      this.failures++;
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    let opened = false;
    ws.onopen = () => {
      opened = true;
      this.retry = 0;
      this.setState('open');
    };
    ws.onmessage = (msg) => {
      if (typeof msg.data !== 'string') return;
      let event: HubEvent;
      try {
        event = JSON.parse(msg.data) as HubEvent;
      } catch {
        return;
      }
      if (!event || typeof event.type !== 'string') return;
      event.data ??= {};
      this.deliver(event);
    };
    ws.onclose = () => {
      this.ws = null;
      if (!opened) this.failures++;
      else this.failures = 0;
      this.setState('closed');
      this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    if (this.options.poll && this.failures >= (this.options.maxFailures ?? 3)) {
      this.startPolling();
      return;
    }
    const delay = Math.min(30_000, 1000 * 2 ** this.retry) + Math.random() * 500;
    this.retry = Math.min(this.retry + 1, 6);
    this.timer = window.setTimeout(() => this.connect(), delay);
  }

  private startPolling() {
    if (this.stopped || this.pollTimer !== undefined) return;
    this.setState('polling');
    const tick = () => {
      if (this.stopped) return;
      void this.pollOnce().finally(() => {
        if (!this.stopped) this.pollTimer = window.setTimeout(tick, this.options.pollMs ?? 10_000);
      });
    };
    this.pollTimer = window.setTimeout(tick, 0);
  }

  private stopPolling() {
    window.clearTimeout(this.pollTimer);
    this.pollTimer = undefined;
    this.pollCtrl?.abort();
    this.pollCtrl = null;
  }

  private async pollOnce(): Promise<void> {
    const poll = this.options.poll;
    if (!poll) return;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden' && this.seeded) return;
    const ctrl = new AbortController();
    this.pollCtrl = ctrl;
    try {
      const events = (await poll(ctrl.signal)).filter((e) => e && typeof e.type === 'string');
      if (this.stopped || ctrl.signal.aborted) return;
      if (this.state !== 'polling') this.setState('polling');
      for (const e of events) {
        e.data ??= {};
        if (this.seeded) {
          this.deliver(e);
        } else {
          // First page is history: keep it for the event log without replaying it to listeners.
          if (e.id) this.seen.add(e.id);
          this.recentEvents = [e, ...this.recentEvents].slice(0, 200);
        }
      }
      this.seeded = true;
    } catch {
      // Transient network or auth failure: try again on the next tick.
    } finally {
      if (this.pollCtrl === ctrl) this.pollCtrl = null;
    }
  }
}
