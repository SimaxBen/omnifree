import { createContext, useCallback, useContext, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Button, cx, haptic } from './ui';

/**
 * Bottom sheet: slides up with a spring, drags down to dismiss (from the grab handle or header),
 * closes on Escape or a backdrop tap, and stays mounted for its exit animation.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
  full,
  headerAction,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  /** Tall sheet (editors, viewers) instead of fitting its content. */
  full?: boolean;
  headerAction?: ReactNode;
}) {
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ y: number; t: number; dy: number } | null>(null);
  const returnFocus = useRef<Element | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (open) {
      returnFocus.current = document.activeElement;
      setMounted(true);
      setClosing(false);
    } else if (mounted) {
      setClosing(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const t = window.setTimeout(() => panelRef.current?.focus({ preventScroll: true }), 30);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!mounted) return null;

  const finish = () => {
    if (!closing) return;
    setMounted(false);
    setClosing(false);
    const el = returnFocus.current;
    if (el instanceof HTMLElement) el.focus({ preventScroll: true });
  };

  const onPointerDown = (e: ReactPointerEvent) => {
    if (e.button !== 0) return;
    drag.current = { y: e.clientY, t: performance.now(), dy: 0 };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: ReactPointerEvent) => {
    const d = drag.current;
    const panel = panelRef.current;
    if (!d || !panel) return;
    // Rubber band upwards, free downwards.
    const raw = e.clientY - d.y;
    d.dy = raw < 0 ? raw / 6 : raw;
    panel.style.transition = 'none';
    panel.style.transform = `translate3d(0, ${d.dy}px, 0)`;
  };
  const onPointerUp = () => {
    const d = drag.current;
    const panel = panelRef.current;
    drag.current = null;
    if (!d || !panel) return;
    const velocity = d.dy / Math.max(performance.now() - d.t, 1);
    if (d.dy > 120 || (d.dy > 30 && velocity > 0.6)) {
      haptic(6);
      panel.style.setProperty('--drag', `${d.dy}px`);
      panel.style.transform = '';
      panel.style.transition = '';
      onClose();
    } else {
      panel.style.transition = 'transform 260ms var(--ease-spring)';
      panel.style.transform = 'translate3d(0, 0, 0)';
    }
  };

  return createPortal(
    <div className="fixed inset-0 z-50" role="presentation">
      <div className={cx('absolute inset-0 bg-[#05060f]/70 backdrop-blur-[2px]', closing ? 'backdrop-exit' : 'backdrop-enter')} onClick={onClose} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        tabIndex={-1}
        onAnimationEnd={(e) => {
          if (e.target === e.currentTarget) finish();
        }}
        className={cx(
          'absolute inset-x-0 bottom-0 mx-auto flex max-w-xl flex-col rounded-t-sheet bg-ink-900 shadow-[0_-12px_48px_-12px_rgba(0,0,0,0.6)] ring-1 ring-ink-800 focus:outline-none',
          full ? 'h-[94dvh]' : 'max-h-[90dvh]',
          closing ? 'sheet-exit' : 'sheet-enter',
        )}
      >
        <div className="shrink-0 cursor-grab touch-none select-none" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}>
          <div className="mx-auto mt-2.5 h-1.5 w-10 rounded-full bg-ink-600" />
          {(title || headerAction) && (
            <div className="flex min-h-[52px] items-center gap-2 px-5 pt-1">
              <h2 className="min-w-0 flex-1 truncate font-display text-[20px] font-semibold text-ink-100">{title}</h2>
              <div onPointerDown={(e) => e.stopPropagation()}>{headerAction}</div>
            </div>
          )}
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pb-5 pt-2">{children}</div>
        {footer && <div className="pb-safe shrink-0 border-t border-ink-800 px-5 pt-3 [&>*]:mb-3">{footer}</div>}
        {!footer && <div className="pb-safe shrink-0" />}
      </div>
    </div>,
    document.body,
  );
}

// ------------------------------------------------------------------ confirm sheet

export interface ConfirmOptions {
  title: string;
  body?: ReactNode;
  confirmLabel: string;
  tone?: 'danger' | 'primary';
}

type ConfirmFn = (o: ConfirmOptions) => Promise<boolean>;
const ConfirmContext = createContext<ConfirmFn | null>(null);

/** In-page confirmation (a bottom sheet) instead of window.confirm. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);
  const [open, setOpen] = useState(false);
  const confirm = useCallback<ConfirmFn>(
    (o) =>
      new Promise<boolean>((resolve) => {
        setCurrent({ ...o, resolve });
        setOpen(true);
      }),
    [],
  );
  const settle = (v: boolean) => {
    current?.resolve(v);
    setOpen(false);
  };
  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {current && (
        <Sheet open={open} onClose={() => settle(false)} title={current.title}>
          {current.body && <div className="pb-2 text-[15px] text-ink-300">{current.body}</div>}
          <div className="flex flex-col gap-2 pt-3">
            <Button
              variant={current.tone === 'danger' ? 'danger' : 'primary'}
              className={current.tone === 'danger' ? 'bg-danger-400 text-ink-950 hover:bg-danger-400' : ''}
              onClick={() => {
                haptic(12);
                settle(true);
              }}
            >
              {current.confirmLabel}
            </Button>
            <Button variant="ghost" onClick={() => settle(false)}>
              Cancel
            </Button>
          </div>
        </Sheet>
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error('useConfirm must be used inside <ConfirmProvider>');
  return ctx;
}
