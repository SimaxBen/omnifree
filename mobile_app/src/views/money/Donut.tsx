import { useEffect, useState } from 'react';
import type { CategorySlice } from '../../lib/apps';
import type { ExpenseCategory } from '../../lib/types';
import { CATEGORY_COLOR } from './ExpenseEditor';

/** Donut of category shares; segments sweep in when the data changes. */
export function Donut({ slices, total, currency }: { slices: CategorySlice[]; total: number; currency: string }) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const [shown, setShown] = useState(false);
  useEffect(() => {
    setShown(false);
    const t = window.requestAnimationFrame(() => setShown(true));
    return () => window.cancelAnimationFrame(t);
  }, [slices]);
  let offset = 0;
  return (
    <svg viewBox="0 0 140 140" className="h-40 w-40 shrink-0" role="img" aria-label={`Spending by category: ${slices.map((s) => `${s.category} ${Math.round(s.share * 100)}%`).join(', ')}`}>
      <circle cx="70" cy="70" r={r} fill="none" stroke="#1C2146" strokeWidth="16" />
      <g transform="rotate(-90 70 70)">
        {slices.map((s) => {
          const len = Math.max(s.share * c - 2, 0.5);
          const el = (
            <circle
              key={s.category}
              cx="70"
              cy="70"
              r={r}
              fill="none"
              stroke={CATEGORY_COLOR[s.category as ExpenseCategory] ?? '#8A8FBF'}
              strokeWidth="16"
              strokeDasharray={`${shown ? len : 0} ${c}`}
              strokeDashoffset={-offset}
              style={{ transition: 'stroke-dasharray 600ms cubic-bezier(0.22, 1, 0.36, 1)' }}
            />
          );
          offset += s.share * c;
          return el;
        })}
      </g>
      <text x="70" y="66" textAnchor="middle" className="fill-ink-400" style={{ font: '500 10px "Atkinson Hyperlegible Next", sans-serif' }}>
        {currency}
      </text>
      <text x="70" y="84" textAnchor="middle" className="fill-ink-100" style={{ font: '700 17px "Bricolage Grotesque", sans-serif' }}>
        {total >= 10000 ? `${(total / 1000).toFixed(1)}k` : Math.round(total).toLocaleString('en-GB')}
      </text>
    </svg>
  );
}
