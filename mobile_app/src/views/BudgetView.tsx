import { Badge, EmptyState, ErrorNote, Meter, SkeletonList, cx, riseStyle } from '../components/ui';
import { groupBudget } from '../lib/apps';
import { relativeTime } from '../lib/files';
import { formatDateTime, formatQuantity } from '../lib/format';
import { useHubData } from '../lib/hub';
import { usePullRefresh } from '../lib/nav';
import type { BudgetItem, BudgetStatus } from '../lib/types';

const TONE: Record<BudgetStatus, 'ok' | 'warn' | 'bad' | 'off'> = { ok: 'ok', warn: 'warn', over: 'bad', unknown: 'off' };
const LABEL: Record<BudgetStatus, string> = { ok: 'fine', warn: 'close', over: 'over', unknown: 'no data' };
const KIND_TITLE: Record<string, string> = { llm: 'Language models', storage: 'Storage', db: 'Databases', cron: 'Schedules', dns: 'DNS', compute: 'Compute', other: 'Other' };

function Row({ item }: { item: BudgetItem }) {
  const tone = TONE[item.status];
  return (
    <div className="px-4 py-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-[15px] text-ink-100">
          {item.provider} <span className="text-[13px] text-ink-400">{item.metric}{item.window ? ` per ${item.window}` : ''}</span>
        </span>
        {item.status !== 'ok' && <Badge tone={tone === 'off' ? 'neutral' : tone}>{LABEL[item.status]}</Badge>}
      </div>
      <Meter value={item.fraction} tone={tone} className="mt-2" />
      <p className="mt-1.5 flex justify-between gap-2 text-[12px] text-ink-500">
        <span>
          {item.used !== null ? formatQuantity(item.used, item.unit) : '?'} of {item.limit !== null ? formatQuantity(item.limit, item.unit) : '?'} {item.unit !== 'bytes' ? item.unit : ''}
        </span>
        <span>{item.reset_at ? `resets ${formatDateTime(item.reset_at)}` : item.source !== 'provider' ? `from ${item.source}` : ''}</span>
      </p>
    </div>
  );
}

export default function BudgetView() {
  const budget = useHubData((c, s) => c.budget(s), [], 120_000);
  usePullRefresh(budget.reload);
  const s = budget.data?.summary;
  const groups = groupBudget(budget.data?.items ?? []);
  return (
    <div className="space-y-5">
      {s && (
        <div className="grid grid-cols-4 gap-2">
          {(
            [
              ['over', 'Over', 'text-danger-400'],
              ['warn', 'Close', 'text-amber-400'],
              ['ok', 'Fine', 'text-mint-400'],
              ['unknown', 'No data', 'text-ink-400'],
            ] as const
          ).map(([k, label, color], i) => (
            <div key={k} className="rise rounded-row bg-ink-900 p-3 text-center ring-1 ring-inset ring-ink-800" style={riseStyle(i)}>
              <p className={cx('font-display text-[26px] font-bold leading-none', s[k] ? color : 'text-ink-600')}>{s[k]}</p>
              <p className="pt-1 text-[12px] text-ink-400">{label}</p>
            </div>
          ))}
        </div>
      )}
      {budget.data?.generated_at && <p className="-mt-2 px-1 text-[12px] text-ink-500">Checked {relativeTime(budget.data.generated_at)}. Close means 80% used.</p>}
      {budget.error && <ErrorNote message={budget.error} onRetry={budget.reload} />}
      {budget.loading && !budget.data && <SkeletonList rows={5} />}
      {budget.data && !groups.length && <EmptyState icon="gauge" title="No limits reported" hint="Enable plugins and their quotas appear here." />}
      {groups.map((g, i) => (
        <section key={g.kind} className="rise space-y-2" style={riseStyle(i + 4)}>
          <h2 className="flex items-center gap-2 px-1 font-display text-[16px] font-semibold text-ink-100">
            {KIND_TITLE[g.kind] ?? g.kind}
            {g.worst !== 'ok' && g.worst !== 'unknown' && <Badge tone={TONE[g.worst] as 'warn' | 'bad'}>{LABEL[g.worst]}</Badge>}
          </h2>
          <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {g.items.map((it) => (
              <Row key={`${it.provider}-${it.metric}-${it.window}-${it.source}`} item={it} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
