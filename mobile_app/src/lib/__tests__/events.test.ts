import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventStream, type ConnectionState } from '../events';
import type { HubEvent } from '../types';

class FailingSocket {
  static created = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  constructor() {
    FailingSocket.created++;
    // Serverless hub: the upgrade is refused (HTTP 403), so the socket closes without opening.
    setTimeout(() => this.onclose?.(), 0);
  }
  close() {}
}

const ev = (id: string, type = 'router.failover'): HubEvent => ({ id, type, ts: '2026-01-01T00:00:00Z', data: {} });

beforeEach(() => {
  vi.useFakeTimers();
  FailingSocket.created = 0;
  vi.stubGlobal('window', globalThis);
  vi.stubGlobal('WebSocket', FailingSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('EventStream polling fallback', () => {
  it('switches to polling after 3 failed handshakes and only dispatches new events', async () => {
    const pages: HubEvent[][] = [[ev('a'), ev('b')], [ev('a'), ev('b'), ev('c')], [ev('b'), ev('c'), ev('d', 'quota.updated')]];
    const poll = vi.fn(async () => pages.shift() ?? []);
    const stream = new EventStream('wss://hub.example/api/ws', undefined, { poll, pollMs: 10_000 });
    const states: ConnectionState[] = [];
    stream.onState((s) => states.push(s));
    const got: string[] = [];
    stream.on(['router.*', 'quota.*'], (e) => got.push(e.id));

    stream.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FailingSocket.created).toBe(3);
    expect(stream.state).toBe('polling');
    expect(states).not.toContain('open');
    expect(poll.mock.calls.length).toBeGreaterThanOrEqual(3);
    // The first page is history (kept for the log); later pages dispatch unseen events only.
    expect(got).toEqual(['c', 'd']);
    expect(stream.recent.map((e) => e.id)).toEqual(['d', 'c', 'b', 'a']);

    stream.stop();
    const calls = poll.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(poll.mock.calls.length).toBe(calls);
    expect(FailingSocket.created).toBe(3);
    expect(stream.state).toBe('closed');
  });

  it('keeps reconnecting the WebSocket when no poll function is given', async () => {
    const stream = new EventStream('wss://hub.example/ws');
    stream.start();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(FailingSocket.created).toBeGreaterThan(3);
    expect(stream.state).not.toBe('polling');
    stream.stop();
  });
});
