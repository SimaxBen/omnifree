import { useEffect, useRef, useState } from 'react';
import { Icon } from '../components/icons';
import { cx, haptic, riseStyle } from '../components/ui';
import { useActions } from '../lib/actions';
import { errorText, useHub } from '../lib/hub';
import { useNav } from '../lib/nav';
import { MIN_QUERY, RESULT_ICON, highlightParts, loadRecent, pushRecent, queryTerms, saveRecent } from '../lib/search';
import { writePref } from '../lib/settings';
import type { Expense, Income, SearchResponse, SearchResult, StoredObject } from '../lib/types';

const DEBOUNCE_MS = 250;

function Highlight({ text, terms }: { text: string; terms: string[] }) {
  return (
    <>
      {highlightParts(text, terms).map((p, i) =>
        p.hit ? (
          <mark key={i} className="rounded-[3px] bg-signal/25 text-ink-100">
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </>
  );
}

function detailOf(r: SearchResult): string {
  const d = r.data as Record<string, unknown>;
  if (r.type === 'expenses' || r.type === 'incomes') {
    const amount = Number(r.type === 'expenses' ? d.total : d.amount) || 0;
    return `${amount.toLocaleString('fr-FR', { maximumFractionDigits: 2 })} ${String(d.currency ?? 'MAD')} · ${r.date ?? ''}`;
  }
  if (r.type === 'tasks') return `${d.done ? 'Done' : 'Open'}${d.due ? ` · due ${String(d.due)}${d.due_time ? ` ${String(d.due_time)}` : ''}` : ''}`;
  return r.date ? r.date.slice(0, 10) : '';
}

/** Full-screen search across the hub (GET /search) with recent searches; each result opens its item. */
export default function SearchScreen({ onClose }: { onClose: (after?: () => void) => void }) {
  const { client, notify } = useHub();
  const actions = useActions();
  const { route, go } = useNav();
  const [q, setQ] = useState('');
  const [recent, setRecent] = useState<string[]>(() => loadRecent());
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const terms = queryTerms(q);
  const ready = q.trim().length >= MIN_QUERY && terms.length > 0;

  useEffect(() => {
    const t = window.setTimeout(() => inputRef.current?.focus(), 60);
    return () => window.clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!ready) {
      setResult(null);
      setLoading(false);
      setError(null);
      return undefined;
    }
    const ctrl = new AbortController();
    setLoading(true);
    const t = window.setTimeout(() => {
      client
        .search(q.trim(), {}, ctrl.signal)
        .then((r) => {
          setResult(r);
          setError(null);
        })
        .catch((err) => {
          if (!ctrl.signal.aborted) setError(errorText(err));
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [q, ready, client]);

  const remember = () => {
    const next = pushRecent(recent, q);
    setRecent(next);
    saveRecent(next);
  };

  const open = (r: SearchResult) => {
    haptic(6);
    remember();
    const d = r.data;
    if (r.type === 'notes') {
      onClose(() => {
        client
          .note(r.id)
          .then((n) => actions.openNote(n))
          .catch((err) => notify(errorText(err), 'error'));
      });
    } else if (r.type === 'tasks') onClose(() => go('notes', 'tasks'));
    else if (r.type === 'expenses') onClose(() => actions.editExpense(d as unknown as Expense));
    else if (r.type === 'incomes') onClose(() => actions.editIncome(d as unknown as Income));
    else if (r.type === 'files') onClose(() => actions.preview(d as unknown as StoredObject));
    else if (r.type === 'links') onClose(() => go('more', 'links'));
    else if (r.type === 'chats') {
      writePref('chat.current', r.id);
      // The chat panel reopens `chat.current` when it mounts: leave the AI tab first when already on it.
      onClose(() => {
        if (route.tab === 'ai') {
          go('home');
          window.setTimeout(() => go('ai'), 0);
        } else go('ai');
      });
    }
  };

  return (
    <div className="pt-safe fixed inset-0 z-40 flex flex-col bg-ink-950" role="dialog" aria-modal="true" aria-label="Search">
      <form
        className="mx-auto flex w-full max-w-xl shrink-0 items-center gap-1 px-2 py-2"
        onSubmit={(e) => {
          e.preventDefault();
          remember();
          inputRef.current?.blur();
        }}
      >
        <button type="button" onClick={() => onClose()} aria-label="Close search" className="press grid h-11 w-11 shrink-0 place-items-center rounded-full text-ink-200 hover:bg-ink-800">
          <Icon name="left" size={22} />
        </button>
        <div className="relative min-w-0 flex-1">
          <Icon name="search" size={18} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-500" />
          <input
            ref={inputRef}
            type="text"
            inputMode="search"
            enterKeyHint="search"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Notes, tasks, money, files, chats…"
            aria-label="Search everything"
            className="min-h-[46px] w-full rounded-full bg-ink-850 pl-10 pr-10 text-[16px] text-ink-100 ring-1 ring-inset ring-ink-700 placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-signal/70"
          />
          {loading ? (
            <span className="spin absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 rounded-full border-2 border-ink-600 border-t-signal" />
          ) : (
            q && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => {
                  setQ('');
                  inputRef.current?.focus();
                }}
                className="press absolute right-1 top-1/2 grid h-10 w-10 -translate-y-1/2 place-items-center rounded-full text-ink-400"
              >
                <Icon name="x" size={16} />
              </button>
            )
          )}
        </div>
      </form>

      <div className="scroll-thin mx-auto w-full max-w-xl flex-1 overflow-y-auto px-4 pb-10">
        {!ready && (
          <section className="pt-2">
            {recent.length > 0 ? (
              <>
                <div className="flex min-h-[40px] items-center justify-between px-1">
                  <h2 className="font-display text-[15px] font-semibold text-ink-400">Recent searches</h2>
                  <button
                    type="button"
                    className="press min-h-[40px] px-2 text-[13px] text-ink-400 hover:text-ink-100"
                    onClick={() => {
                      setRecent([]);
                      saveRecent([]);
                    }}
                  >
                    Clear
                  </button>
                </div>
                <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                  {recent.map((r) => (
                    <li key={r}>
                      <button type="button" onClick={() => setQ(r)} className="press flex min-h-[52px] w-full items-center gap-3 px-4 text-left hover:bg-ink-850">
                        <Icon name="clock" size={18} className="shrink-0 text-ink-500" />
                        <span className="min-w-0 flex-1 truncate text-[15px] text-ink-100">{r}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="px-1 pt-6 text-center text-[14px] text-ink-500">Search notes, tasks, expenses, income, Drive files, AI chats and links. Accents don’t matter.</p>
            )}
          </section>
        )}

        {ready && error && <p className="px-1 pt-4 text-[14px] text-danger-400">{error}</p>}
        {ready && result && !error && result.groups.length === 0 && !loading && (
          <p className="px-1 pt-6 text-center text-[14px] text-ink-400">Nothing matches “{q.trim()}”.</p>
        )}
        {ready && result && Object.keys(result.errors).length > 0 && (
          <p className="px-1 pt-2 text-[12px] text-amber-400">Not searched: {Object.keys(result.errors).join(', ')}</p>
        )}

        {ready &&
          result?.groups.map((g, gi) => (
            <section key={g.type} className="rise pt-4" style={riseStyle(gi)}>
              <h2 className="flex items-baseline gap-2 px-1 pb-2 font-display text-[15px] font-semibold text-ink-400">
                {g.label}
                <span className="text-[12px] font-normal text-ink-500">{g.count > g.items.length ? `${g.items.length} of ${g.count}` : g.count}</span>
              </h2>
              <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                {g.items.map((r) => (
                  <li key={r.id}>
                    <button type="button" onClick={() => open(r)} className="press flex min-h-[60px] w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-ink-850 active:bg-ink-850">
                      <span className={cx('mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-ink-800 text-ink-200')}>
                        <Icon name={RESULT_ICON[r.type]} size={18} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[15px] text-ink-100">
                          <Highlight text={r.title} terms={result.terms} />
                        </span>
                        {r.snippet && (
                          <span className="line-clamp-2 block text-[13px] leading-snug text-ink-400">
                            <Highlight text={r.snippet} terms={result.terms} />
                          </span>
                        )}
                        {detailOf(r) && <span className="block text-[12px] text-ink-500">{detailOf(r)}</span>}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
      </div>
    </div>
  );
}
