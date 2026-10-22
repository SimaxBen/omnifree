import { useEffect, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Button, ErrorNote, Spinner, cx, inputClass, riseStyle } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { formatMoney } from '../../lib/apps';
import { errorText, useHub } from '../../lib/hub';
import { highlight } from '../../lib/moneyTools';
import type { ExpenseSearchHit, ExpenseSearchResult } from '../../lib/moneyTypes';
import { CATEGORY_COLOR } from './ExpenseEditor';

const DEBOUNCE_MS = 350;

function dayLabel(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

/** The receipt behind a hit: its presigned URL, else downloaded through the hub (bearer token) as a blob URL. */
function useReceiptUrl(hit: ExpenseSearchHit | null): { url: string | null; loading: boolean; error: string | null } {
  const { client } = useHub();
  const [state, setState] = useState<{ url: string | null; loading: boolean; error: string | null }>({ url: null, loading: false, error: null });
  useEffect(() => {
    if (!hit?.receipt_key) return undefined;
    if (hit.receipt_url) {
      setState({ url: hit.receipt_url, loading: false, error: null });
      return undefined;
    }
    if (!hit.receipt_provider) {
      setState({ url: null, loading: false, error: 'This receipt is not in the Drive any more.' });
      return undefined;
    }
    let objectUrl: string | null = null;
    let live = true;
    setState({ url: null, loading: true, error: null });
    client
      .driveDownload(hit.receipt_key, hit.receipt_provider)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (live) setState({ url: objectUrl, loading: false, error: null });
      })
      .catch((err) => live && setState({ url: null, loading: false, error: errorText(err) }));
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [client, hit]);
  return state;
}

function ReceiptPreview({ hit, open, onClose }: { hit: ExpenseSearchHit; open: boolean; onClose: () => void }) {
  const actions = useActions();
  const receipt = useReceiptUrl(hit);
  return (
    <Sheet
      open={open}
      onClose={onClose}
      full
      title={hit.merchant || 'Receipt'}
      footer={
        <div className="flex gap-2">
          {receipt.url && (
            <a href={receipt.url} target="_blank" rel="noopener noreferrer" className="press inline-flex min-h-[44px] items-center justify-center gap-2 rounded-full bg-ink-800 px-4 text-[15px] text-ink-100">
              <Icon name="external" size={18} />
              Open
            </a>
          )}
          <Button
            variant="primary"
            icon="edit"
            className="flex-1"
            onClick={() => {
              onClose();
              actions.editExpense(hit);
            }}
          >
            Edit expense
          </Button>
        </div>
      }
    >
      <p className="pb-3 text-[14px] text-ink-400">
        {dayLabel(hit.date)} · <span className="capitalize">{hit.category}</span> · {formatMoney(hit.total, hit.currency)}
      </p>
      {receipt.error && <ErrorNote message={receipt.error} />}
      {receipt.loading && (
        <div className="grid h-64 place-items-center">
          <Spinner />
        </div>
      )}
      {receipt.url && hit.receipt_kind === 'pdf' && <iframe title="Receipt PDF" src={receipt.url} className="h-[62dvh] w-full rounded-card bg-white" />}
      {receipt.url && hit.receipt_kind !== 'pdf' && <img src={receipt.url} alt={`Receipt from ${hit.merchant}`} className="mx-auto max-h-[62dvh] w-full rounded-card bg-ink-950 object-contain" />}
      {!hit.receipt_key && <p className="text-[14px] text-ink-400">No receipt image for this expense.</p>}
    </Sheet>
  );
}

function Thumb({ hit }: { hit: ExpenseSearchHit }) {
  const tint = CATEGORY_COLOR[hit.category] ?? '#8A8FBF';
  if (hit.receipt_url && hit.receipt_kind === 'image') {
    return <img src={hit.receipt_url} alt="" loading="lazy" className="h-14 w-11 shrink-0 rounded-[10px] bg-ink-850 object-cover ring-1 ring-inset ring-ink-700" />;
  }
  return (
    <span className="grid h-14 w-11 shrink-0 place-items-center rounded-[10px]" style={{ background: `${tint}22`, color: tint }}>
      <Icon name={hit.receipt_kind === 'pdf' ? 'file' : hit.receipt_key ? 'receipt' : 'wallet'} size={18} />
    </span>
  );
}

/** Search box of the Expenses page: merchant, note, items and the receipts' text, with thumbnails. */
export function ReceiptSearch({ onActive }: { onActive: (active: boolean) => void }) {
  const { client } = useHub();
  const [q, setQ] = useState('');
  const [result, setResult] = useState<ExpenseSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ hit: ExpenseSearchHit; open: boolean; id: number } | null>(null);
  const [tick, setTick] = useState(0);
  const active = q.trim().length >= 2;

  useEffect(() => {
    onActive(active);
  }, [active, onActive]);

  useEffect(() => {
    if (!active) {
      // Cleared while a search was running: that request is aborted below, so nothing else stops the spinner.
      setLoading(false);
      setResult(null);
      setError(null);
      return undefined;
    }
    const ctrl = new AbortController();
    const t = window.setTimeout(() => {
      setLoading(true);
      client
        .searchExpenses(q.trim(), { limit: 40 }, ctrl.signal)
        .then((r) => {
          setResult(r);
          setError(null);
        })
        .catch((err) => {
          if (!(err instanceof DOMException && err.name === 'AbortError')) setError(errorText(err));
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      // A newer query (or a cleared box) replaces this one; the next effect run decides the spinner.
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [client, q, active, tick]);

  return (
    <div className="space-y-3">
      <label className="relative block">
        <span className="sr-only">Search expenses and receipts</span>
        <Icon name="search" size={18} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-500" />
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search receipts: lait, glovo, pharmacie…" className={cx(inputClass, 'pl-10 pr-10')} />
        {loading && <Spinner className="absolute right-3.5 top-1/2 -translate-y-1/2" />}
      </label>
      {active && error && <ErrorNote message={error} onRetry={() => setTick((n) => n + 1)} />}
      {active && result && !result.results.length && !loading && <p className="px-1 text-[14px] text-ink-400">Nothing matches “{q.trim()}”.</p>}
      {active && result && result.results.length > 0 && (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {result.results.map((hit, i) => (
            <button key={hit.id} type="button" onClick={() => setPreview({ hit, open: true, id: Date.now() })} className="press rise flex min-h-[72px] w-full items-center gap-3 px-3 py-2 text-left hover:bg-ink-850" style={riseStyle(i)}>
              <Thumb hit={hit} />
              <span className="min-w-0 flex-1">
                <span className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-[15px] text-ink-100">{hit.merchant || 'Untitled'}</span>
                  <span className="shrink-0 font-display text-[15px] font-semibold tabular-nums text-ink-100">{formatMoney(hit.total, hit.currency)}</span>
                </span>
                <span className="block truncate text-[12px] text-ink-500">
                  {dayLabel(hit.date)} · <span className="capitalize">{hit.category}</span>
                </span>
                {hit.snippet && (
                  <span className="block truncate text-[12px] text-ink-400">
                    {highlight(hit.snippet, result.terms).map((part, j) => (part.hit ? <mark key={j} className="rounded bg-signal/25 px-0.5 text-ink-100">{part.text}</mark> : <span key={j}>{part.text}</span>))}
                  </span>
                )}
              </span>
            </button>
          ))}
        </div>
      )}
      {preview && <ReceiptPreview key={preview.id} hit={preview.hit} open={preview.open} onClose={() => setPreview((p) => (p ? { ...p, open: false } : p))} />}
    </div>
  );
}
