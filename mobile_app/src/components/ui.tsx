import type { ButtonHTMLAttributes, CSSProperties, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { formatQuantity, fraction } from '../lib/format';
import type { QuotaInfo } from '../lib/types';
import { Icon, type IconName } from './icons';

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

/** A short vibration where the platform supports it (Android); silently nothing elsewhere. */
export function haptic(pattern: number | number[] = 8): void {
  try {
    if (typeof navigator !== 'undefined' && 'vibrate' in navigator) navigator.vibrate(pattern);
  } catch {
    // Vibration is a nicety.
  }
}

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

/** Stagger index for `.rise` list items, capped so long lists do not wait. */
export function riseStyle(i: number): CSSProperties {
  return { ['--i' as string]: Math.min(i, 8) } as CSSProperties;
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cx('rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800', className)}>{children}</section>;
}

export function SectionTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex min-h-[44px] items-center justify-between gap-2 px-1 pb-1">
      <h2 className="font-display text-[17px] font-semibold text-ink-100">{children}</h2>
      {action}
    </div>
  );
}

type Variant = 'primary' | 'ghost' | 'danger' | 'subtle';

export function Button({
  variant = 'subtle',
  icon,
  children,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; icon?: IconName }) {
  const styles: Record<Variant, string> = {
    primary: 'bg-signal text-ink-950 font-semibold hover:brightness-105',
    subtle: 'bg-ink-800 text-ink-100 hover:bg-ink-700',
    ghost: 'text-ink-300 hover:text-ink-100 hover:bg-ink-800',
    danger: 'bg-danger-400/15 text-danger-400 hover:bg-danger-400/20',
  };
  return (
    <button
      type="button"
      className={cx(
        'press inline-flex min-h-[44px] items-center justify-center gap-2 rounded-full px-4 text-[15px]',
        'disabled:cursor-not-allowed disabled:opacity-40',
        styles[variant],
        className,
      )}
      {...rest}
    >
      {icon && <Icon name={icon} size={18} />}
      {children}
    </button>
  );
}

export function IconButton({ icon, label, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cx(
        'press inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-ink-300 hover:bg-ink-800 hover:text-ink-100',
        'disabled:cursor-not-allowed disabled:opacity-30',
        className,
      )}
      {...rest}
    >
      <Icon name={icon} size={20} />
    </button>
  );
}

export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => {
        haptic(6);
        onChange(!checked);
      }}
      className={cx(
        'relative inline-flex h-8 w-[52px] shrink-0 items-center rounded-full transition-colors duration-base disabled:opacity-40',
        checked ? 'bg-signal' : 'bg-ink-700',
      )}
    >
      <span
        className={cx(
          'inline-block h-6 w-6 rounded-full shadow transition-transform duration-slow ease-spring',
          checked ? 'translate-x-[24px] bg-ink-950' : 'translate-x-1 bg-ink-300',
        )}
      />
    </button>
  );
}

export function StatusDot({ tone, pulse }: { tone: 'ok' | 'warn' | 'bad' | 'off'; pulse?: boolean }) {
  const color = { ok: 'bg-mint-400', warn: 'bg-amber-400', bad: 'bg-danger-400', off: 'bg-ink-500' }[tone];
  return (
    <span className="relative inline-flex h-2.5 w-2.5 shrink-0">
      {pulse && <span className={cx('absolute inline-flex h-full w-full animate-ping rounded-full opacity-40', color)} />}
      <span className={cx('relative inline-flex h-2.5 w-2.5 rounded-full', color)} />
    </span>
  );
}

export function Badge({ children, tone = 'neutral', className }: { children: ReactNode; tone?: 'neutral' | 'ok' | 'warn' | 'bad' | 'signal'; className?: string }) {
  const styles = {
    neutral: 'bg-ink-800 text-ink-300',
    ok: 'bg-mint-400/15 text-mint-400',
    signal: 'bg-signal/15 text-signal',
    warn: 'bg-amber-400/15 text-amber-400',
    bad: 'bg-danger-400/15 text-danger-400',
  }[tone];
  return <span className={cx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[12px] leading-5', styles, className)}>{children}</span>;
}

/** A selectable pill (filters, notebooks, categories). */
export function Chip({ active, children, onClick, className }: { active?: boolean; children: ReactNode; onClick?: () => void; className?: string }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={() => {
        haptic(5);
        onClick?.();
      }}
      className={cx(
        'press inline-flex min-h-[36px] shrink-0 items-center gap-1.5 rounded-full px-3.5 text-[14px]',
        active ? 'bg-ink-100 text-ink-950 font-semibold' : 'bg-ink-850 text-ink-300 ring-1 ring-inset ring-ink-700 hover:text-ink-100',
        className,
      )}
    >
      {children}
    </button>
  );
}

export function toneForFraction(f: number | null): 'ok' | 'warn' | 'bad' | 'off' {
  if (f === null) return 'off';
  if (f >= 1) return 'bad';
  if (f >= 0.8) return 'warn';
  return 'ok';
}

const BAR_TONE = { ok: 'bg-mint-400', warn: 'bg-amber-400', bad: 'bg-danger-400', off: 'bg-ink-500' } as const;

export function Meter({ value, tone, className }: { value: number | null; tone?: 'ok' | 'warn' | 'bad' | 'off'; className?: string }) {
  const t = tone ?? toneForFraction(value);
  return (
    <div className={cx('h-2 overflow-hidden rounded-full bg-ink-800', className)}>
      <div
        className={cx('h-full rounded-full transition-[width] duration-slow ease-out', BAR_TONE[t])}
        style={{ width: `${value === null ? 0 : Math.max(Math.min(value, 1) * 100, 2)}%` }}
      />
    </div>
  );
}

export function QuotaBar({ quota, label }: { quota: QuotaInfo; label?: string }) {
  const f = fraction(quota.used, quota.limit);
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2 text-[13px]">
        <span className="truncate text-ink-300">
          {label ?? `${quota.unit}${quota.window ? ` per ${quota.window}` : ''}`}
          {quota.source !== 'provider' && <span className="ml-1 text-ink-500">({quota.source})</span>}
        </span>
        <span className="shrink-0 text-ink-100">
          {formatQuantity(quota.used, quota.unit)}
          <span className="text-ink-500"> of {quota.limit ? formatQuantity(quota.limit, quota.unit) : 'unknown'}</span>
        </span>
      </div>
      <Meter value={f} tone={f === null ? 'off' : f >= 0.9 ? 'bad' : f >= 0.7 ? 'warn' : 'ok'} />
    </div>
  );
}

/** Ring gauge for the most constrained quota of a provider. */
export function Gauge({ value, size = 56, label }: { value: number | null; size?: number; label: string }) {
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const stroke = value === null ? '#5A6197' : value >= 0.9 ? '#FF5C7A' : value >= 0.7 ? '#FF9548' : '#5EE0A8';
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={label}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#1C2146" strokeWidth={5} />
        {value !== null && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={stroke}
            strokeWidth={5}
            strokeLinecap="round"
            strokeDasharray={`${c * value} ${c}`}
            className="transition-[stroke-dasharray] duration-slow"
          />
        )}
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-[12px] font-semibold text-ink-200">
        {value === null ? '--' : `${Math.round(value * 100)}%`}
      </span>
    </div>
  );
}

export function EmptyState({ icon, title, hint, action }: { icon: IconName; title: string; hint?: ReactNode; action?: ReactNode }) {
  return (
    <div className="screen-in-up flex flex-col items-center gap-2 px-6 py-10 text-center">
      <span className="mb-1 grid h-14 w-14 place-items-center rounded-full bg-ink-850 text-ink-400">
        <Icon name={icon} size={24} />
      </span>
      <p className="font-display text-[17px] font-semibold text-ink-100">{title}</p>
      {hint && <p className="max-w-xs text-[14px] text-ink-400">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start gap-3 rounded-row bg-danger-400/10 px-4 py-3 text-[14px] text-danger-400">
      <p className="min-w-0 flex-1 break-words">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="press -my-2 min-h-[40px] shrink-0 font-semibold underline underline-offset-4">
          Retry
        </button>
      )}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <span className={cx('spin inline-block h-4 w-4 rounded-full border-2 border-ink-600 border-t-signal', className)} role="status" aria-label="Loading" />;
}

/** Placeholder rows while a list loads. */
export function SkeletonList({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cx('space-y-2', className)} aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-row bg-ink-900 p-3">
          <div className="skeleton h-10 w-10 shrink-0 rounded-full" />
          <div className="flex-1 space-y-2">
            <div className="skeleton h-3.5" style={{ width: `${70 - ((i * 13) % 30)}%` }} />
            <div className="skeleton h-3" style={{ width: `${45 - ((i * 7) % 20)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  const idx = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );
  return (
    <div className="relative flex rounded-full bg-ink-900 p-1 ring-1 ring-inset ring-ink-800" role="tablist">
      <span
        aria-hidden="true"
        className="absolute bottom-1 top-1 rounded-full bg-ink-700 transition-transform duration-slow ease-spring"
        style={{ width: `calc((100% - 8px) / ${options.length})`, transform: `translateX(${idx * 100}%)` }}
      />
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          onClick={() => {
            if (o.value !== value) haptic(5);
            onChange(o.value);
          }}
          className={cx(
            'relative z-10 min-h-[40px] flex-1 rounded-full px-2 text-[14px] transition-colors duration-base',
            value === o.value ? 'font-semibold text-ink-100' : 'text-ink-400',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A tappable row in a grouped list (settings-style). */
export function ListRow({
  icon,
  title,
  detail,
  trailing,
  onClick,
  tone = 'neutral',
}: {
  icon?: IconName;
  title: ReactNode;
  detail?: ReactNode;
  trailing?: ReactNode;
  onClick?: () => void;
  tone?: 'neutral' | 'signal' | 'warn' | 'bad';
}) {
  const iconTone = { neutral: 'bg-ink-800 text-ink-200', signal: 'bg-signal/15 text-signal', warn: 'bg-amber-400/15 text-amber-400', bad: 'bg-danger-400/15 text-danger-400' }[tone];
  const body = (
    <>
      {icon && (
        <span className={cx('grid h-10 w-10 shrink-0 place-items-center rounded-full', iconTone)}>
          <Icon name={icon} size={20} />
        </span>
      )}
      <span className="min-w-0 flex-1 text-left">
        <span className="block truncate text-[15px] text-ink-100">{title}</span>
        {detail && <span className="block truncate text-[13px] text-ink-400">{detail}</span>}
      </span>
      {trailing}
    </>
  );
  if (!onClick) return <div className="flex min-h-[60px] items-center gap-3 px-4 py-2">{body}</div>;
  return (
    <button type="button" onClick={onClick} className="press flex min-h-[60px] w-full items-center gap-3 px-4 py-2 hover:bg-ink-850 active:bg-ink-850">
      {body}
      {!trailing && <Icon name="right" size={18} className="text-ink-500" />}
    </button>
  );
}

export const inputClass =
  'w-full min-h-[46px] rounded-row bg-ink-850 px-4 text-[16px] text-ink-100 ring-1 ring-inset ring-ink-700 placeholder:text-ink-500 transition-shadow duration-fast focus:outline-none focus:ring-2 focus:ring-signal/70';

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="block px-1 text-[13px] text-ink-300">{label}</span>
      {children}
      {hint && <span className="block px-1 text-[12px] text-ink-500">{hint}</span>}
    </label>
  );
}

/**
 * Floating action button above the bottom navigation, in a portal: screen transitions animate
 * `transform`, which would otherwise make the screen the containing block of a fixed element.
 */
export function Fab({ icon, label, onClick }: { icon: IconName; label: string; onClick: () => void }) {
  if (typeof document === 'undefined') return null;
  return createPortal(
    <button
      type="button"
      onClick={() => {
        haptic(8);
        onClick();
      }}
      aria-label={label}
      title={label}
      className="press pop fixed z-20 grid h-14 w-14 place-items-center rounded-[20px] bg-ink-100 text-ink-950 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]"
      style={{ right: 'max(16px, calc((100vw - 36rem) / 2 + 16px))', bottom: 'calc(var(--nav-h) + var(--safe-bottom) + 24px)' }}
    >
      <Icon name={icon} size={22} />
    </button>,
    document.body,
  );
}
