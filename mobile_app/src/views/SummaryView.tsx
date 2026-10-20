import { useMemo, useState } from 'react';
import { Icon, type IconName } from '../components/icons';
import { Markdown } from '../components/Markdown';
import { Badge, Chip, EmptyState, ErrorNote, IconButton, Meter, Segmented, Spinner, cx, riseStyle } from '../components/ui';
import { categoryBreakdown, currenciesByWeight, formatMoney } from '../lib/apps';
import { ago, providerBars, rangeLabel, taskShares } from '../lib/digest';
import { formatBytes } from '../lib/format';
import { errorText, useHub, useHubData } from '../lib/hub';
import { usePullRefresh, useNav } from '../lib/nav';
import type { Digest, DigestPeriod, ExpenseCategory } from '../lib/types';
import { Donut } from './money/Donut';
import { CATEGORY_COLOR } from './money/ExpenseEditor';

const PERIODS: { value: DigestPeriod; label: string }[] = [
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
];

function Tile({ icon, label, value, detail, tone, i }: { icon: IconName; label: string; value: string; detail?: string; tone: string; i: number }) {
  return (
    <div className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(i)}>
      <span className={cx('grid h-9 w-9 place-items-center rounded-full', tone)}>
        <Icon name={icon} size={18} />
      </span>
      <p className="pt-3 font-display text-[26px] font-bold leading-none text-ink-100">{value}</p>
      <p className="pt-1 text-[13px] text-ink-400">{label}</p>
      {detail && <p className="truncate text-[12px] text-ink-500">{detail}</p>}
    </div>
  );
}

function Spending({ digest }: { digest: Digest }) {
  const { go } = useNav();
  const e = digest.stats.expenses;
  const currencies = currenciesByWeight(e.total_by_currency);
  const [currency, setCurrency] = useState<string | null>(null);
  const cur = currency && e.total_by_currency[currency] !== undefined ? currency : currencies[0];
  const slices = useMemo(() => (cur ? categoryBreakdown(e.by_category, cur) : []), [e.by_category, cur]);
  if (!cur) {
    return (
      <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
        <p className="font-display text-[17px] font-semibold text-ink-100">No spending recorded</p>
        <p className="text-[14px] text-ink-400">Scan receipts or import them from Gmail on the Expenses page.</p>
        <button type="button" onClick={() => go('more', 'expenses')} className="press mt-2 min-h-[44px] text-[14px] text-signal">
          Open Expenses
        </button>
      </section>
    );
  }
  return (
    <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(1)}>
      <div className="flex items-center justify-between gap-2 pb-3">
        <h2 className="font-display text-[17px] font-semibold text-ink-100">Spending</h2>
        <span className="text-[13px] text-ink-400">
          {e.count} expense{e.count === 1 ? '' : 's'}
        </span>
      </div>
      {currencies.length > 1 && (
        <div className="no-scrollbar -mx-1 mb-3 flex gap-2 overflow-x-auto px-1">
          {currencies.map((c) => (
            <Chip key={c} active={c === cur} onClick={() => setCurrency(c)}>
              {formatMoney(e.total_by_currency[c], c)}
            </Chip>
          ))}
        </div>
      )}
      <div className="flex items-center gap-4">
        <Donut slices={slices} total={e.total_by_currency[cur] ?? 0} currency={cur} />
        <ul className="min-w-0 flex-1 space-y-2">
          {slices.slice(0, 6).map((s) => (
            <li key={s.category} className="flex items-center justify-between gap-2 text-[13px]">
              <span className="flex min-w-0 items-center gap-1.5 text-ink-200">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CATEGORY_COLOR[s.category as ExpenseCategory] ?? '#8A8FBF' }} />
                <span className="truncate capitalize">{s.category}</span>
              </span>
              <span className="shrink-0 text-ink-400">{Math.round(s.share * 100)}%</span>
            </li>
          ))}
        </ul>
      </div>
      {e.top_merchants.length > 0 && (
        <ul className="mt-4 divide-y divide-ink-800 border-t border-ink-800">
          {e.top_merchants.slice(0, 4).map((m) => (
            <li key={`${m.merchant}-${m.currency}`} className="flex min-h-[44px] items-center justify-between gap-3 text-[14px]">
              <span className="min-w-0 truncate text-ink-200">
                {m.merchant}
                {m.count > 1 && <span className="text-ink-500"> ×{m.count}</span>}
              </span>
              <span className="shrink-0 font-display font-semibold text-ink-100">{formatMoney(m.total, m.currency)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Tasks({ digest }: { digest: Digest }) {
  const t = digest.stats.tasks;
  const share = taskShares(t);
  const legend = [
    { label: 'Done', value: t.done, color: 'bg-mint-400' },
    { label: 'Open', value: Math.max(t.open - t.overdue, 0), color: 'bg-signal' },
    { label: 'Overdue', value: t.overdue, color: 'bg-danger-400' },
  ];
  return (
    <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(2)}>
      <h2 className="pb-3 font-display text-[17px] font-semibold text-ink-100">Tasks</h2>
      <div className="flex h-3 overflow-hidden rounded-full bg-ink-800" role="img" aria-label={`${t.done} done, ${t.open} open, ${t.overdue} overdue`}>
        <span className="h-full bg-mint-400 transition-[width] duration-slow ease-out" style={{ width: `${share.done * 100}%` }} />
        <span className="h-full bg-signal transition-[width] duration-slow ease-out" style={{ width: `${share.open * 100}%` }} />
        <span className="h-full bg-danger-400 transition-[width] duration-slow ease-out" style={{ width: `${share.overdue * 100}%` }} />
      </div>
      <div className="grid grid-cols-3 gap-2 pt-3">
        {legend.map((l) => (
          <div key={l.label}>
            <p className="font-display text-[22px] font-semibold leading-tight text-ink-100">{l.value}</p>
            <p className="flex items-center gap-1.5 text-[13px] text-ink-400">
              <span className={cx('h-2 w-2 rounded-full', l.color)} />
              {l.label}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function Bullets({ title, icon, items, tone, start }: { title: string; icon: IconName; items: string[]; tone: string; start: number }) {
  if (!items.length) return null;
  return (
    <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(start)}>
      <h2 className="flex items-center gap-2 pb-2 font-display text-[17px] font-semibold text-ink-100">
        <span className={cx('grid h-7 w-7 place-items-center rounded-full', tone)}>
          <Icon name={icon} size={15} />
        </span>
        {title}
      </h2>
      <ul className="space-y-2">
        {items.map((h, i) => (
          <li key={i} className="flex gap-2 text-[15px] leading-snug text-ink-200">
            <span className="mt-[9px] h-1.5 w-1.5 shrink-0 rounded-full bg-ink-500" />
            <span className="min-w-0">{h}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function SummaryView() {
  const { client, notify } = useHub();
  const [period, setPeriod] = useState<DigestPeriod>('week');
  const [refreshing, setRefreshing] = useState(false);
  const data = useHubData((c, s) => c.digest(period, false, s), [period]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const fresh = await client.digest(period, true);
      data.setData(() => fresh);
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setRefreshing(false);
    }
  };
  usePullRefresh(refresh);

  const d = data.data?.period === period ? data.data : undefined;
  const llm = d ? providerBars(d.stats.llm.requests_by_provider) : [];
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <div className="flex-1">
          <Segmented value={period} options={PERIODS} onChange={setPeriod} />
        </div>
        <IconButton
          icon="refresh"
          label="Regenerate the summary"
          onClick={() => void refresh()}
          disabled={refreshing || data.loading}
          className={cx('bg-ink-900 ring-1 ring-inset ring-ink-800', refreshing && 'spin')}
        />
      </div>

      {d && (
        <p key={`${d.from}-${d.generated_at}`} className="screen-in-up flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-[13px] text-ink-400">
          <span className="font-semibold text-ink-200">{rangeLabel(d.from, d.to)}</span>
          <span>· updated {ago(d.generated_at)}</span>
          {d.source === 'template' && <Badge>no AI</Badge>}
        </p>
      )}

      {data.error && <ErrorNote message={data.error} onRetry={data.reload} />}
      {data.loading && !d && (
        <div className="space-y-3" aria-hidden="true">
          <div className="skeleton h-32 rounded-card" />
          <div className="skeleton h-48 rounded-card" />
          <div className="grid grid-cols-2 gap-3">
            <div className="skeleton h-32 rounded-card" />
            <div className="skeleton h-32 rounded-card" />
          </div>
        </div>
      )}
      {data.loading && !d && (
        <p className="flex items-center justify-center gap-2 text-[13px] text-ink-500">
          <Spinner /> Writing your summary…
        </p>
      )}

      {d && (
        <>
          {d.markdown && (
            <section className="rise rounded-card bg-signal/[0.07] p-4 ring-1 ring-inset ring-signal/20" style={riseStyle(0)}>
              <Markdown text={d.markdown} className="text-[15px] leading-relaxed text-ink-100" />
            </section>
          )}
          <Spending digest={d} />
          <Tasks digest={d} />
          <div className="grid grid-cols-2 gap-3">
            <Tile
              i={3}
              icon="note"
              label="Notes written"
              value={String(d.stats.notes.created)}
              detail={d.stats.notes.voice ? `${d.stats.notes.voice} by voice` : undefined}
              tone="bg-signal/15 text-signal"
            />
            <Tile
              i={4}
              icon="upload"
              label="Files uploaded"
              value={d.stats.drive.uploaded_files === null ? '–' : String(d.stats.drive.uploaded_files)}
              detail={d.stats.drive.uploaded_bytes ? formatBytes(d.stats.drive.uploaded_bytes) : undefined}
              tone="bg-[#7FA8FF]/15 text-[#A9CBFF]"
            />
          </div>
          <Bullets title="Highlights" icon="spark" items={d.highlights} tone="bg-mint-400/15 text-mint-400" start={5} />
          <Bullets title="Suggestions" icon="flag" items={d.suggestions} tone="bg-amber-400/15 text-amber-400" start={6} />
          {llm.length > 0 && (
            <section className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(7)}>
              <h2 className="pb-3 font-display text-[17px] font-semibold text-ink-100">AI requests</h2>
              <ul className="space-y-2.5">
                {llm.slice(0, 6).map((p) => (
                  <li key={p.provider}>
                    <span className="flex justify-between gap-2 text-[13px]">
                      <span className="truncate text-ink-200">{p.provider}</span>
                      <span className="shrink-0 text-ink-400">{p.count}</span>
                    </span>
                    <Meter value={p.share} tone="ok" className="mt-1 h-1.5" />
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
      {!data.loading && !d && !data.error && <EmptyState icon="chart" title="No summary yet" hint="Pull down to try again." />}
    </div>
  );
}
