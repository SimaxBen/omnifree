import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { HubClient } from './api';
import { EventStream, SUBSCRIBED_TYPES, type ConnectionState } from './events';
import { OfflineHubClient } from './offline/client';
import { offline } from './offline/manager';
import { loadSettings, normalizeHubUrl, saveSettings, type HubSettings } from './settings';
import type { HubEvent } from './types';

export interface Toast {
  id: number;
  tone: 'info' | 'error' | 'ok';
  text: string;
}

interface HubContextValue {
  settings: HubSettings;
  updateSettings: (s: HubSettings) => boolean;
  client: HubClient;
  events: EventStream;
  connection: ConnectionState;
  toasts: Toast[];
  notify: (text: string, tone?: Toast['tone']) => void;
  dismiss: (id: number) => void;
}

const HubContext = createContext<HubContextValue | null>(null);

export function HubProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<HubSettings>(() => loadSettings());
  // Offline-aware: cached lists when the hub is out of reach, queued writes replayed later (lib/offline).
  const client = useMemo<HubClient>(() => new OfflineHubClient(settings), [settings]);
  const events = useMemo(
    () =>
      new EventStream(client.wsUrl(SUBSCRIBED_TYPES), client.wsProtocols(), {
        poll: (signal) => client.recentEvents(100, SUBSCRIBED_TYPES, signal),
      }),
    [client],
  );
  const [connection, setConnection] = useState<ConnectionState>('idle');
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  useEffect(() => {
    const stop = offline.start();
    return stop;
  }, []);

  useEffect(() => {
    const off = events.onState(setConnection);
    events.start();
    return () => {
      off();
      events.stop();
    };
  }, [events]);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  const notify = useCallback(
    (text: string, tone: Toast['tone'] = 'info') => {
      const id = nextId.current++;
      setToasts((t) => [...t.slice(-3), { id, tone, text }]);
      window.setTimeout(() => dismiss(id), tone === 'error' ? 7000 : 3500);
    },
    [dismiss],
  );

  const updateSettings = useCallback((s: HubSettings) => {
    const persisted = saveSettings(s);
    setSettings(persisted ? loadSettings() : { hubUrl: normalizeHubUrl(s.hubUrl), token: s.token });
    return persisted;
  }, []);

  const value = useMemo(
    () => ({ settings, updateSettings, client, events, connection, toasts, notify, dismiss }),
    [settings, updateSettings, client, events, connection, toasts, notify, dismiss],
  );
  return <HubContext.Provider value={value}>{children}</HubContext.Provider>;
}

export function useHub(): HubContextValue {
  const ctx = useContext(HubContext);
  if (!ctx) throw new Error('useHub must be used inside <HubProvider>');
  return ctx;
}

/** Subscribe to hub events matching `patterns` for the lifetime of the component. */
export function useHubEvents(patterns: string[], handler: (e: HubEvent) => void): void {
  const { events } = useHub();
  const ref = useRef(handler);
  ref.current = handler;
  const key = patterns.join('|');
  useEffect(() => {
    const off = events.on(key.split('|'), (e) => ref.current(e));
    return () => off();
  }, [events, key]);
}

interface AsyncState<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => void;
  setData: (fn: (prev: T | undefined) => T | undefined) => void;
}

/** Loads data from the hub, re-running when `deps` or the hub client change; optional polling. */
export function useHubData<T>(
  load: (client: HubClient, signal: AbortSignal) => Promise<T>,
  deps: unknown[] = [],
  pollMs?: number,
): AsyncState<T> {
  const { client } = useHub();
  const [data, setDataState] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loader = useRef(load);
  loader.current = load;

  useEffect(() => {
    const ctrl = new AbortController();
    let first = true;
    const run = async () => {
      if (first) setLoading(true);
      first = false;
      try {
        const result = await loader.current(client, ctrl.signal);
        if (!ctrl.signal.aborted) {
          setDataState(result);
          setError(null);
        }
      } catch (err) {
        if (!ctrl.signal.aborted && !(err instanceof DOMException && err.name === 'AbortError')) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!ctrl.signal.aborted) setLoading(false);
      }
    };
    void run();
    const interval = pollMs ? window.setInterval(() => {
      if (document.visibilityState === 'visible') void run();
    }, pollMs) : undefined;
    return () => {
      ctrl.abort();
      window.clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, tick, pollMs, ...deps]);

  // Reload when queued writes synced or live data is back after showing saved data.
  useEffect(() => {
    const off = offline.onRefresh(() => setTick((t) => t + 1));
    return off;
  }, []);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const setData = useCallback((fn: (prev: T | undefined) => T | undefined) => setDataState(fn), []);
  return { data, error, loading, reload, setData };
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
