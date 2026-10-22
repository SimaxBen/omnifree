import { Icon } from '../../components/icons';
import { cx, riseStyle } from '../../components/ui';
import { formatMoney } from '../../lib/apps';
import { voiceDebtLine } from '../../lib/moneyTools';
import type { VoiceDebtOutcome } from '../../lib/moneyTypes';

/** The loans and repayments a voice memo recorded ("j'ai prêté 200 dh à Ayoub"), with Undo when it stored any. */
export function VoiceDebts({ debts, onUndo, undoing = false }: { debts: VoiceDebtOutcome[]; onUndo?: () => void; undoing?: boolean }) {
  if (!debts.length) return null;
  return (
    <div className="space-y-1">
      <div className="flex min-h-[44px] items-center justify-between gap-2 px-1">
        <h3 className="font-display text-[16px] font-semibold text-ink-100">Debts</h3>
        {onUndo && (
          <button
            type="button"
            onClick={onUndo}
            disabled={undoing}
            className="press flex min-h-[44px] shrink-0 items-center gap-1 text-[14px] font-semibold text-signal disabled:opacity-40"
          >
            <Icon name="undo" size={15} />
            {undoing ? 'Undoing…' : 'Undo'}
          </button>
        )}
      </div>
      <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
        {debts.map((d, i) => {
          const owed = d.type === 'lend' || d.type === 'repay' || d.type === 'split';
          const left = d.debts.reduce((a, x) => a + (x.settled ? 0 : x.remaining), 0);
          const warn = d.action === 'unmatched' || d.action === 'duplicate';
          return (
            <div key={`${d.person}-${i}`} className="rise flex min-h-[56px] items-center gap-3 px-3 py-2" style={riseStyle(i + 1)}>
              <span className={cx('grid h-9 w-9 shrink-0 place-items-center rounded-full', warn ? 'bg-amber-400/15 text-amber-400' : 'bg-signal/15 text-signal')}>
                <Icon name={warn ? 'flag' : d.type === 'split' ? 'cash' : owed ? 'up' : 'down'} size={16} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[15px] leading-snug text-ink-100">{voiceDebtLine(d)}</span>
                {d.action === 'repaid' && <span className="block truncate text-[12px] text-ink-500">{left > 0 ? `${formatMoney(left, d.currency)} still open` : 'Fully settled'}</span>}
                {d.type === 'split' && d.action === 'created' && d.split && (
                  <span className="block truncate text-[12px] text-ink-500">
                    Your share {formatMoney(d.split.my_share, d.currency)} · the rest is on the Debts page
                  </span>
                )}
              </span>
              <span className={cx('shrink-0 font-display text-[16px] font-semibold tabular-nums', warn ? 'text-ink-500' : 'text-ink-100')}>{formatMoney(d.amount, d.currency)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
