// "Open this item" requests across screens: global search asks for a task or a link that lives in a list screen.
// The request waits here until that screen is mounted (it may still be loading after the navigation) and is then
// handed to it exactly once; a screen that is already showing gets it right away through its listener.

import { useEffect, useRef } from 'react';

export type FocusKind = 'task' | 'link' | 'drop';

export interface FocusRequest<T = unknown> {
  kind: FocusKind;
  id: string;
  /** What the requester already knows about the item (a search hit's data). */
  data?: T;
}

/** A request nobody picked up within this time is dropped (the screen it targeted was never opened). */
export const FOCUS_TTL_MS = 30_000;

const pending = new Map<FocusKind, FocusRequest & { at: number }>();
const listeners = new Set<(kind: FocusKind) => void>();

/** Asks the screen showing `kind` items to open `id`. */
export function requestFocus(req: FocusRequest, now: number = Date.now()): void {
  pending.set(req.kind, { ...req, at: now });
  for (const l of [...listeners]) l(req.kind);
}

/** The waiting request for `kind`, removed so it is handled once. */
export function takeFocus<T = unknown>(kind: FocusKind, now: number = Date.now()): FocusRequest<T> | null {
  const req = peekFocus<T>(kind, now);
  pending.delete(kind);
  return req;
}

/** Keeps the waiting request for `kind` for later (the screen is not ready to open it yet). */
export function peekFocus<T = unknown>(kind: FocusKind, now: number = Date.now()): FocusRequest<T> | null {
  const req = pending.get(kind);
  if (!req) return null;
  if (now - req.at > FOCUS_TTL_MS) {
    pending.delete(kind);
    return null;
  }
  return { kind: req.kind, id: req.id, data: req.data as T };
}

/** Runs `handle` for each request of `kind` while mounted (and for one already waiting at mount). Returning false
 *  leaves the request waiting, so the screen can retry once its data has loaded. */
export function useFocusRequest<T = unknown>(kind: FocusKind, handle: (req: FocusRequest<T>) => boolean | void, deps: unknown[] = []): void {
  const ref = useRef(handle);
  ref.current = handle;
  useEffect(() => {
    const run = (k: FocusKind) => {
      if (k !== kind) return;
      const req = peekFocus<T>(kind);
      if (req && ref.current(req) !== false) takeFocus(kind);
    };
    run(kind);
    listeners.add(run);
    return () => {
      listeners.delete(run);
    };
    // deps: re-check a waiting request when the screen's data arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, ...deps]);
}

/** Scrolls the element marked `data-focus="<id>"` into view and flashes it. */
export function flashRow(id: string): void {
  window.setTimeout(() => {
    const el = document.querySelector<HTMLElement>(`[data-focus="${CSS.escape(id)}"]`);
    if (!el) return;
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    el.classList.add('focus-flash');
    window.setTimeout(() => el.classList.remove('focus-flash'), 1600);
  }, 80);
}
