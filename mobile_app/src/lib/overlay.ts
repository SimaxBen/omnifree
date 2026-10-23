import { useCallback, useEffect, useRef } from 'react';

// Full-screen overlays (search, the file viewer, a file opened inside a zip) that the Android back
// gesture closes: opening one pushes a history entry with the same URL, so a back gesture pops it
// and closes that overlay. Overlays stack (a zip inside a zip inside the viewer): one back gesture
// closes only the top one, through a single shared popstate listener. Closing from the UI goes
// through the returned `close(after)`, which pops the entry first and only then runs `after`
// (typically a navigation), so the navigation's own history entry is never the one popped.

export interface BackEntry {
  close: () => void;
  /** Unmounted without its history entry being popped: popped as soon as it reaches the top. */
  dead: boolean;
}

const stack: BackEntry[] = [];
/** Pops this module asked for itself (history.go), with what to run when each lands. */
const expected: { at: number; run: (() => void) | null }[] = [];
const EXPECTED_TTL_MS = 2000;
let listening = false;

function go(n: number, run: (() => void) | null): void {
  expected.push({ at: Date.now(), run });
  try {
    window.history.go(-n);
  } catch {
    expected.pop();
    run?.();
  }
}

/** Pops dead entries off the top (plus `extra` entries the caller already removed), in one go. */
function settle(extra: number, run: (() => void) | null): void {
  let n = extra;
  while (stack.length && stack[stack.length - 1].dead) {
    stack.pop();
    n++;
  }
  if (n > 0) go(n, run);
  else run?.();
}

function onPop(): void {
  while (expected.length && Date.now() - expected[0].at > EXPECTED_TTL_MS) expected.shift(); // lost traversals
  const mine = expected.shift();
  if (mine) {
    mine.run?.();
    return;
  }
  // A back gesture: close the top overlay only.
  const top = stack.pop();
  if (!top) return;
  top.close();
  settle(0, null);
}

export function isLive(entry: BackEntry): boolean {
  return !entry.dead && stack.includes(entry);
}

/** Registers an open overlay: one history entry, closed by the next back gesture while on top. */
export function pushBackEntry(close: () => void): BackEntry {
  const entry: BackEntry = { close, dead: false };
  try {
    window.history.pushState({ omnifreeOverlay: true }, '', window.location.href);
  } catch {
    return { close, dead: true }; // history unavailable (sandboxed previews): never live
  }
  if (!listening) {
    window.addEventListener('popstate', onPop);
    listening = true;
  }
  stack.push(entry);
  return entry;
}

/** Closes from the UI: pops this entry (and any above it), then closes them, top first, then `after`. */
export function backTo(entry: BackEntry, after?: () => void): void {
  const i = stack.indexOf(entry);
  if (i < 0 || entry.dead) {
    entry.close();
    after?.();
    return;
  }
  const removed = stack.splice(i);
  settle(removed.length, () => {
    for (const e of removed.reverse()) if (!e.dead) e.close();
    if (after) window.setTimeout(after, 0);
  });
}

/** The overlay went away without a back gesture (unmounted): drop its history entry. */
export function releaseBackEntry(entry: BackEntry): void {
  if (!stack.includes(entry)) return;
  entry.dead = true;
  settle(0, null);
}

/** Test hook. */
export function backStackDepth(): number {
  return stack.length;
}

/**
 * While `open`, the back gesture closes the overlay. A React StrictMode remount (cleanup + effect
 * in the same tick) keeps the same history entry.
 */
export function useOverlayBack(open: boolean, onClose: () => void): (after?: () => void) => void {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const entryRef = useRef<BackEntry | null>(null);
  const releaseTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!open) return undefined;
    window.clearTimeout(releaseTimer.current);
    if (!entryRef.current || !isLive(entryRef.current)) entryRef.current = pushBackEntry(() => closeRef.current());
    const entry = entryRef.current;
    return () => {
      releaseTimer.current = window.setTimeout(() => {
        releaseBackEntry(entry);
        if (entryRef.current === entry) entryRef.current = null;
      }, 0);
    };
  }, [open]);

  return useCallback((after?: () => void) => {
    const entry = entryRef.current;
    if (entry && isLive(entry)) {
      entryRef.current = null;
      backTo(entry, after);
      return;
    }
    closeRef.current();
    after?.();
  }, []);
}
