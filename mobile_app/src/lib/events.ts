// One shared WebSocket to hub_api /ws with exponential-backoff reconnect and
// pattern-based listeners ("router.*", "instinct.task.screenshot", "*").

import type { HubEvent } from './types';

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'closed';

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

  constructor(
    private readonly url: string,
    private readonly protocols?: string[],
  ) {}

  get state(): ConnectionState {
    return this._state;
  }

  get recent(): HubEvent[] {
    return this.recentEvents;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    window.clearTimeout(this.timer);
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
    return () => this.listeners.delete(entry);
  }

  onState(fn: (s: ConnectionState) => void): () => void {
    this.stateListeners.add(fn);
    return () => this.stateListeners.delete(fn);
  }

  private setState(s: ConnectionState) {
    this._state = s;
    this.stateListeners.forEach((fn) => fn(s));
  }

  private connect() {
    if (this.stopped) return;
    this.setState('connecting');
    let ws: WebSocket;
    try {
      ws = this.protocols ? new WebSocket(this.url, this.protocols) : new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
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
      this.recentEvents = [event, ...this.recentEvents].slice(0, 200);
      for (const l of this.listeners) {
        if (l.patterns.some((p) => matchesPattern(event.type, p))) l.fn(event);
      }
    };
    ws.onclose = () => {
      this.ws = null;
      this.setState('closed');
      this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    const delay = Math.min(30_000, 1000 * 2 ** this.retry) + Math.random() * 500;
    this.retry = Math.min(this.retry + 1, 6);
    this.timer = window.setTimeout(() => this.connect(), delay);
  }
}
