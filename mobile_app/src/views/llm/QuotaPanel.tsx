import { useRef } from 'react';
import { Card, EmptyState, ErrorNote, Gauge, IconButton, QuotaBar, SectionTitle, Spinner } from '../../components/ui';
import { formatTime, fraction } from '../../lib/format';
import { useHubData, useHubEvents } from '../../lib/hub';
import type { QuotaInfo } from '../../lib/types';

function tightest(list: QuotaInfo[]): number | null {
  let max: number | null = null;
  for (const q of list) {
    const f = fraction(q.used, q.limit);
    if (f !== null && (max === null || f > max)) max = f;
  }
  return max;
}

export default function QuotaPanel() {
  const quotas = useHubData((c, s) => c.quotas(s), [], 60_000);
  const timer = useRef<number | undefined>(undefined);

  // Router usage changes arrive as events; coalesce them into one refresh.
  useHubEvents(['quota.*', 'router.success'], () => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(quotas.reload, 1500);
  });

  const entries = Object.entries(quotas.data ?? {}).sort(([, a], [, b]) => (tightest(b) ?? -1) - (tightest(a) ?? -1));

  return (
    <div>
      <SectionTitle action={quotas.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh quotas" onClick={quotas.reload} />}>
        Per-provider quotas
      </SectionTitle>
      {quotas.error && <ErrorNote message={quotas.error} onRetry={quotas.reload} />}
      {!quotas.loading && !quotas.error && entries.length === 0 && (
        <EmptyState icon="pulse" title="No quota data yet" hint="Counters appear after the first routed request, or when providers report usage." />
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        {entries.map(([name, list]) => {
          const exhausted = list.find((q) => q.limit && q.used >= q.limit);
          return (
            <Card key={name} className="p-3">
              <div className="mb-3 flex items-center gap-3">
                <Gauge value={tightest(list)} label={`${name} tightest quota`} />
                <div className="min-w-0">
                  <p className="truncate font-mono text-sm text-ink-100">{name}</p>
                  <p className="text-xs text-ink-400">
                    {exhausted ? (
                      <span className="text-danger-400">exhausted{exhausted.reset_at ? `, resets ${formatTime(exhausted.reset_at)}` : ''}</span>
                    ) : (
                      `${list.length} counter${list.length === 1 ? '' : 's'}`
                    )}
                  </p>
                </div>
              </div>
              <div className="space-y-2.5">
                {list.map((q, i) => (
                  <QuotaBar key={`${q.unit}-${q.window}-${q.source}-${i}`} quota={q} />
                ))}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
