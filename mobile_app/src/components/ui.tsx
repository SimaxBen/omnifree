import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { formatQuantity, fraction } from '../lib/format';
import type { QuotaInfo } from '../lib/types';
import { Icon, type IconName } from './icons';

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cx('rounded-xl border border-ink-700/70 bg-ink-900/80', className)}>{children}</section>;
}

export function SectionTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-center justify-between px-1 pb-2 pt-1">
      <h2 className="text-[11px] font-medium uppercase tracking-[0.14em] text-ink-400">{children}</h2>
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
    primary: 'bg-signal text-ink-950 hover:bg-signal/90 font-medium',
    subtle: 'bg-ink-800 text-ink-100 hover:bg-ink-700 border border-ink-700',
    ghost: 'text-ink-300 hover:text-ink-100 hover:bg-ink-800',
    danger: 'bg-danger-400/10 text-danger-400 hover:bg-danger-400/20 border border-danger-400/30',
  };
  return (
    <button
      type="button"
      className={cx(
        'inline-flex min-h-[40px] items-center justify-center gap-2 rounded-lg px-3 text-sm transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-signal',
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
        'inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100',
        'disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-signal',
        className,
      )}
      {...rest}
    >
      <Icon name={icon} size={18} />
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
      onClick={() => onChange(!checked)}
      className={cx(
        'relative inline-flex h-7 w-12 shrink-0 items-center rounded-full transition-colors disabled:opacity-40',
        checked ? 'bg-signal' : 'bg-ink-700',
      )}
    >
      <span className={cx('inline-block h-5 w-5 rounded-full bg-ink-950 shadow transition-transform', checked ? 'translate-x-6' : 'translate-x-1')} />
    </button>
  );
}

export function StatusDot({ tone, pulse }: { tone: 'ok' | 'warn' | 'bad' | 'off'; pulse?: boolean }) {
  const color = { ok: 'bg-signal', warn: 'bg-amber-400', bad: 'bg-danger-400', off: 'bg-ink-500' }[tone];
  return (
    <span className="relative inline-flex h-2.5 w-2.5 shrink-0">
      {pulse && <span className={cx('absolute inline-flex h-full w-full animate-ping rounded-full opacity-50', color)} />}
      <span className={cx('relative inline-flex h-2.5 w-2.5 rounded-full', color)} />
    </span>
  );
}

export function Badge({ children, tone = 'neutral', className }: { children: ReactNode; tone?: 'neutral' | 'ok' | 'warn' | 'bad' | 'signal'; className?: string }) {
  const styles = {
    neutral: 'border-ink-600 text-ink-300',
    ok: 'border-signal/40 text-signal',
    signal: 'border-signal/50 bg-signal/10 text-signal',
    warn: 'border-amber-400/40 text-amber-400',
    bad: 'border-danger-400/40 text-danger-400',
  }[tone];
  return <span className={cx('inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[11px] leading-none', styles, className)}>{children}</span>;
}

function barTone(f: number | null): string {
  if (f === null) return 'bg-ink-500';
  if (f >= 0.9) return 'bg-danger-400';
  if (f >= 0.7) return 'bg-amber-400';
  return 'bg-signal';
}

export function QuotaBar({ quota, label }: { quota: QuotaInfo; label?: string }) {
  const f = fraction(quota.used, quota.limit);
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="truncate text-ink-300">
          {label ?? `${quota.unit}${quota.window ? ` / ${quota.window}` : ''}`}
          {quota.source !== 'provider' && <span className="ml-1 text-ink-500">({quota.source})</span>}
        </span>
        <span className="shrink-0 font-mono text-ink-200">
          {formatQuantity(quota.used, quota.unit)}
          <span className="text-ink-500"> / {quota.limit ? formatQuantity(quota.limit, quota.unit) : 'unknown'}</span>
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-ink-800">
        <div className={cx('h-full rounded-full transition-[width] duration-500', barTone(f))} style={{ width: `${f === null ? 0 : Math.max(f * 100, 2)}%` }} />
      </div>
    </div>
  );
}

/** Ring gauge for the most constrained quota of a provider. */
export function Gauge({ value, size = 56, label }: { value: number | null; size?: number; label: string }) {
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const stroke = value === null ? '#4b5669' : value >= 0.9 ? '#ff6b6b' : value >= 0.7 ? '#f5b84a' : '#3ee6a8';
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={label}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#222a38" strokeWidth={5} />
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
            className="transition-[stroke-dasharray] duration-500"
          />
        )}
      </svg>
      <span className="absolute inset-0 flex items-center justify-center font-mono text-[11px] text-ink-200">
        {value === null ? '--' : `${Math.round(value * 100)}%`}
      </span>
    </div>
  );
}

export function EmptyState({ icon, title, hint }: { icon: IconName; title: string; hint?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
      <span className="rounded-full border border-ink-700 p-3 text-ink-400">
        <Icon name={icon} size={22} />
      </span>
      <p className="text-sm text-ink-200">{title}</p>
      {hint && <p className="max-w-xs text-xs text-ink-400">{hint}</p>}
    </div>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-danger-400/30 bg-danger-400/5 px-3 py-2.5 text-sm text-danger-400">
      <p className="min-w-0 flex-1 break-words">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="shrink-0 text-xs underline underline-offset-2 hover:text-ink-100">
          Retry
        </button>
      )}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <span className={cx('inline-block h-4 w-4 animate-spin rounded-full border-2 border-ink-600 border-t-signal', className)} aria-label="Loading" />;
}

export function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex rounded-lg border border-ink-700 bg-ink-900 p-0.5" role="tablist">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          onClick={() => onChange(o.value)}
          className={cx(
            'min-h-[36px] flex-1 rounded-md px-3 text-sm transition-colors',
            value === o.value ? 'bg-ink-700 text-ink-100' : 'text-ink-400 hover:text-ink-200',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const inputClass =
  'w-full min-h-[40px] rounded-lg border border-ink-700 bg-ink-850 px-3 text-sm text-ink-100 placeholder:text-ink-500 focus:border-signal/60 focus:outline-none focus:ring-1 focus:ring-signal/40';
