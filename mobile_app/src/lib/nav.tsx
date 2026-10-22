import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { readPref, writePref } from './settings';

export const TABS = ['home', 'notes', 'drive', 'ai', 'more'] as const;
export type Tab = (typeof TABS)[number];

export interface Route {
  tab: Tab;
  /** The view's own section (a More page, a segment such as 'tasks' or 'ask'). */
  sub: string;
}

export function parseHash(hash: string): Route | null {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  const tab = parts[0] as Tab;
  if (!TABS.includes(tab)) return null;
  return { tab, sub: parts[1] ?? '' };
}

export function routeHash(r: Route): string {
  return `#/${r.tab}${r.sub ? `/${encodeURIComponent(r.sub)}` : ''}`;
}

interface NavValue {
  route: Route;
  /** Index delta of the last tab change (for the slide direction). */
  direction: number;
  go: (tab: Tab, sub?: string) => void;
  /** Changes the section without a new history entry (segment switches). */
  setSub: (sub: string) => void;
  back: () => void;
}

const NavContext = createContext<NavValue | null>(null);

export function NavProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<Route>(() => {
    const fromHash = typeof window !== 'undefined' ? parseHash(window.location.hash) : null;
    return fromHash ?? { tab: readPref<Tab>('tab', 'home'), sub: '' };
  });
  const [direction, setDirection] = useState(0);
  const routeRef = useRef(route);
  routeRef.current = route;

  const apply = useCallback((next: Route) => {
    setDirection(TABS.indexOf(next.tab) - TABS.indexOf(routeRef.current.tab));
    setRoute(next);
    writePref('tab', next.tab);
  }, []);

  useEffect(() => {
    const onPop = () => {
      const r = parseHash(window.location.hash);
      if (r) apply(r);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [apply]);

  const go = useCallback(
    (tab: Tab, sub = '') => {
      const next = { tab, sub };
      if (next.tab === routeRef.current.tab && next.sub === routeRef.current.sub) return;
      try {
        window.history.pushState(null, '', routeHash(next));
      } catch {
        // History may be unavailable (sandboxed previews); navigation still works in memory.
      }
      apply(next);
    },
    [apply],
  );

  const setSub = useCallback((sub: string) => {
    const next = { tab: routeRef.current.tab, sub };
    try {
      window.history.replaceState(null, '', routeHash(next));
    } catch {
      // See go().
    }
    setRoute(next);
  }, []);

  const back = useCallback(() => {
    const cur = routeRef.current;
    if (cur.tab === 'more' && cur.sub.startsWith('money-')) go('more', 'money'); // Money sub-pages go back to Money
    else if (cur.tab === 'more' && cur.sub) go('more', '');
    else go('home');
  }, [go]);

  const value = useMemo(() => ({ route, direction, go, setSub, back }), [route, direction, go, setSub, back]);
  return <NavContext.Provider value={value}>{children}</NavContext.Provider>;
}

export function useNav(): NavValue {
  const ctx = useContext(NavContext);
  if (!ctx) throw new Error('useNav must be used inside <NavProvider>');
  return ctx;
}

// ------------------------------------------------------------------ pull to refresh registry

type RefreshFn = () => Promise<unknown> | void;
const refreshers = new Set<{ fn: RefreshFn }>();

/** Registers what a pull-to-refresh gesture reloads while this component is mounted. */
export function usePullRefresh(fn: RefreshFn): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const entry = { fn: () => ref.current() };
    refreshers.add(entry);
    return () => {
      refreshers.delete(entry);
    };
  }, []);
}

export async function runRefreshers(): Promise<void> {
  await Promise.allSettled([...refreshers].map((r) => Promise.resolve(r.fn())));
}

export function hasRefreshers(): boolean {
  return refreshers.size > 0;
}
