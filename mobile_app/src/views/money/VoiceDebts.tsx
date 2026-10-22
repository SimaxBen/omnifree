import { Icon } from '../../components/icons';
import { cx, riseStyle } from '../../components/ui';
import { formatMoney } from '../../lib/apps';
import { voiceDebtLine } from '../../lib/moneyTools';
import type { VoiceDebtOutcome } from '../../lib/moneyTypes';

/** The loans and repayments a voice memo recorded ("j'ai prêté 200 dh à Ayoub"). */
export function VoiceDebts({ debts }: { debts: VoiceDebtOutcome[] }) {
  if (!debts.length) return null;
  return (
    <div className="space-y-1">
      <h3 className="px-1 font-display text-[16px] font-semibold text-ink-100">Debts</h3>
      <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
        {debts.map((d, i) => {
          const owed = d.type === 'lend' || d.type === 'repay';
          const left = d.debts.reduce((a, x) => a + (x.settled ? 0 : x.remaining), 0);
          return (
            <div key={`${d.person}-${i}`} className="rise flex min-h-[56px] items-center gap-3 px-3 py-2" style={riseStyle(i + 1)}>
              <span className={cx('grid h-9 w-9 shrink-0 place-items-center rounded-full', d.action === 'unmatched' ? 'bg-amber-400/15 text-amber-400' : 'bg-signal/15 text-signal')}>
                <Icon name={d.action === 'unmatched' ? 'flag' : owed ? 'up' : 'down'} size={16} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{voiceDebtLine(d)}</span>
                {d.action === 'repaid' && <span className="block truncate text-[12px] text-ink-500">{left > 0 ? `${formatMoney(left, d.currency)} still open` : 'Fully settled'}</span>}
              </span>
              <span className="shrink-0 font-display text-[16px] font-semibold tabular-nums text-ink-100">{formatMoney(d.amount, d.currency)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
