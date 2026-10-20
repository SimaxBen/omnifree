import { useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { Icon, type IconName } from './icons';
import { cx, haptic, prefersReducedMotion } from './ui';

export interface SwipeAction {
  icon: IconName;
  label: string;
  tone: 'signal' | 'danger' | 'mint';
  /** Return true (or a promise of true) to collapse the row away, e.g. after a delete. */
  run: () => boolean | void | Promise<boolean | void>;
}

const TONE = { signal: 'bg-signal text-ink-950', danger: 'bg-danger-400 text-ink-950', mint: 'bg-mint-400 text-ink-950' } as const;
const TRIGGER = 88;

/**
 * A list row that reveals an action when dragged sideways: right for `start` (complete, pin),
 * left for `end` (delete). Vertical scrolling keeps working (touch-action: pan-y).
 */
export function SwipeRow({ children, start, end, className }: { children: ReactNode; start?: SwipeAction; end?: SwipeAction; className?: string }) {
  const rowRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const g = useRef<{ x: number; y: number; dx: number; locked: 'x' | 'y' | null; armed: boolean } | null>(null);
  const [side, setSide] = useState<'start' | 'end' | null>(null);
  const [armed, setArmed] = useState(false);

  const setX = (x: number, animate: boolean) => {
    const el = rowRef.current;
    if (!el) return;
    el.style.transition = animate ? 'transform 280ms var(--ease-spring)' : 'none';
    el.style.transform = x ? `translate3d(${x}px,0,0)` : '';
  };

  const down = (e: ReactPointerEvent) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    g.current = { x: e.clientX, y: e.clientY, dx: 0, locked: null, armed: false };
  };
  const move = (e: ReactPointerEvent) => {
    const s = g.current;
    if (!s) return;
    const dx = e.clientX - s.x;
    const dy = e.clientY - s.y;
    if (!s.locked) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      s.locked = Math.abs(dx) > Math.abs(dy) * 1.2 ? 'x' : 'y';
      if (s.locked === 'x') (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    }
    if (s.locked !== 'x') return;
    let x = dx;
    if ((x > 0 && !start) || (x < 0 && !end)) x = x / 8;
    s.dx = x;
    setSide(x > 0 ? 'start' : x < 0 ? 'end' : null);
    const isArmed = Math.abs(x) > TRIGGER && !!(x > 0 ? start : end);
    if (isArmed !== s.armed) {
      s.armed = isArmed;
      setArmed(isArmed);
      if (isArmed) haptic(10);
    }
    setX(x, false);
  };
  const up = async () => {
    const s = g.current;
    g.current = null;
    if (!s || s.locked !== 'x') return;
    const action = s.dx > 0 ? start : end;
    const fire = s.armed && !!action;
    setArmed(false);
    setX(0, true);
    if (!fire || !action) return;
    const remove = await action.run();
    if (remove && wrapRef.current && !prefersReducedMotion() && typeof wrapRef.current.animate === 'function') {
      const el = wrapRef.current;
      const anim = el.animate(
        [
          { opacity: 1, height: `${el.offsetHeight}px` },
          { opacity: 0, height: '0px' },
        ],
        { duration: 220, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', fill: 'forwards' },
      );
      await anim.finished.catch(() => undefined);
    }
  };

  const active = side === 'start' ? start : side === 'end' ? end : undefined;
  return (
    <div ref={wrapRef} className={cx('relative overflow-hidden rounded-row', className)}>
      {active && (
        <div
          aria-hidden="true"
          className={cx(
            'absolute inset-0 flex items-center px-6 transition-opacity duration-fast',
            TONE[active.tone],
            side === 'start' ? 'justify-start' : 'justify-end',
            armed ? 'opacity-100' : 'opacity-60',
          )}
        >
          <span className={cx('flex items-center gap-2 font-semibold transition-transform duration-base ease-spring', armed && 'scale-110')}>
            <Icon name={active.icon} size={20} />
            {active.label}
          </span>
        </div>
      )}
      <div
        ref={rowRef}
        className="relative touch-pan-y bg-ink-900"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={() => void up()}
        onPointerCancel={() => void up()}
      >
        {children}
      </div>
    </div>
  );
}
