import { useCallback, useEffect, useRef } from 'react';

// Full-screen overlays (search, share) that the Android back gesture closes: opening one pushes a history entry
// with the same URL, so a back gesture pops it and closes the overlay. Closing from the UI goes through the
// returned `close(after)`, which pops that entry first and only then runs `after` (typically a navigation), so
// the navigation's own history entry is never the one popped.

export function useOverlayBack(open: boolean, onClose: () => void): (after?: () => void) => void {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const pushed = useRef(false);
  const afterRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    try {
      window.history.pushState({ omnifreeOverlay: true }, '', window.location.href);
      pushed.current = true;
    } catch {
      pushed.current = false;
    }
    const onPop = () => {
      if (!pushed.current) return;
      pushed.current = false;
      const after = afterRef.current;
      afterRef.current = null;
      closeRef.current();
      if (after) window.setTimeout(after, 0);
    };
    window.addEventListener('popstate', onPop);
    return () => {
      window.removeEventListener('popstate', onPop);
      if (pushed.current) {
        pushed.current = false;
        try {
          window.history.back();
        } catch {
          // History unavailable (sandboxed previews).
        }
      }
    };
  }, [open]);

  return useCallback((after?: () => void) => {
    if (pushed.current) {
      afterRef.current = after ?? null;
      try {
        window.history.back();
        return;
      } catch {
        pushed.current = false;
      }
    }
    closeRef.current();
    after?.();
  }, []);
}
