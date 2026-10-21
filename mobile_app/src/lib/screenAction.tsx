import { useEffect, useSyncExternalStore, type ReactNode } from 'react';

// One action a screen puts on the right of its large title line (the shell renders the title,
// the view owns the button). A tiny store rather than a context: only the slot re-renders.

let current: ReactNode = null;
const listeners = new Set<() => void>();

function set(node: ReactNode): void {
  current = node;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Shows `node` on the screen's title line while the calling view is mounted. */
export function useScreenAction(node: ReactNode): void {
  useEffect(() => {
    set(node);
    return () => {
      set(null);
    };
  }, [node]);
}

/** Rendered by the shell next to the large title. */
export function ScreenActionSlot() {
  const node = useSyncExternalStore(subscribe, () => current, () => null);
  return node ? <div className="-my-1 -mr-1 shrink-0">{node}</div> : null;
}
