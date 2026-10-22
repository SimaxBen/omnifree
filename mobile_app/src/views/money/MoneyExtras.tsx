import { useRef, useState } from 'react';
import { Icon, type IconName } from '../../components/icons';
import { IconButton, SectionTitle, riseStyle } from '../../components/ui';
import { useTopic } from '../../lib/bus';
import { useHubData } from '../../lib/hub';
import type { Debt, DebtDirection } from '../../lib/moneyTypes';
import { useNav } from '../../lib/nav';
import { DebtRow, DebtSheet, OwedTotals } from './DebtSheets';

const OWED_ROWS = 3;

/** Money page: "Owed": open loans (people who owe me first), with a link to every debt. */
export function OwedSection({ i }: { i: number }) {
  const { go } = useNav();
  const data = useHubData((c, s) => c.debts('open', s), []);
  useTopic(['money'], data.reload);
  const [sheet, setSheet] = useState<{ id: number; open: boolean; debt: Debt | null; direction?: DebtDirection } | null>(null);
  const next = useRef(1);
  const open = (debt: Debt | null, direction?: DebtDirection) => setSheet({ id: next.current++, open: true, debt, direction });
  const debts = [...(data.data?.debts ?? [])].sort((a, b) => (a.direction === b.direction ? a.date.localeCompare(b.date) : a.direction === 'lent' ? -1 : 1));

  return (
    <section className="rise" style={riseStyle(i)}>
      <SectionTitle action={<IconButton icon="plus" label="Add a loan" onClick={() => open(null, 'lent')} />}>Owed</SectionTitle>
      {data.data && debts.length > 0 ? (
        <div className="space-y-2">
          <OwedTotals data={data.data} />
          <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {debts.slice(0, OWED_ROWS).map((d, j) => (
              <DebtRow key={d.id} debt={d} i={j} onClick={() => open(d)} />
            ))}
            <button type="button" onClick={() => go('more', 'money-debts')} className="press flex min-h-[48px] w-full items-center justify-between px-4 text-[14px] text-signal">
              {debts.length > OWED_ROWS ? `All ${debts.length} open loans` : 'History and settled loans'}
              <Icon name="right" size={16} />
            </button>
          </div>
        </div>
      ) : (
        <button type="button" onClick={() => go('more', 'money-debts')} className="press w-full rounded-card border border-dashed border-ink-700 px-4 py-4 text-left text-[14px] text-ink-400">
          Lent or borrowed money? Track it here, or say “j’ai prêté 200 dh à Ayoub”.
        </button>
      )}
      {sheet && <DebtSheet key={sheet.id} open={sheet.open} debt={sheet.debt} direction={sheet.direction} onClose={() => setSheet((s) => (s ? { ...s, open: false } : s))} />}
    </section>
  );
}

function Tile({ icon, label, hint, onClick }: { icon: IconName; label: string; hint: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="press flex min-h-[92px] flex-col items-start justify-between rounded-card bg-ink-900 p-3 text-left ring-1 ring-inset ring-ink-800">
      <span className="grid h-9 w-9 place-items-center rounded-full bg-signal/15 text-signal">
        <Icon name={icon} size={18} />
      </span>
      <span>
        <span className="block text-[15px] text-ink-100">{label}</span>
        <span className="block text-[12px] leading-snug text-ink-500">{hint}</span>
      </span>
    </button>
  );
}

/** Money page: Split and Rules. */
export function MoneyToolTiles({ i }: { i: number }) {
  const { go } = useNav();
  return (
    <section className="rise grid grid-cols-2 gap-2" style={riseStyle(i)}>
      <Tile icon="cash" label="Split" hint="Shared with friends" onClick={() => go('more', 'money-split')} />
      <Tile icon="tag" label="Rules" hint="Auto categories" onClick={() => go('more', 'money-rules')} />
    </section>
  );
}
