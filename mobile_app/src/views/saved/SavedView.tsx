import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Icon } from '../../components/icons';
import { Markdown } from '../../components/Markdown';
import { SwipeRow } from '../../components/SwipeRow';
import { Badge, Button, Chip, EmptyState, ErrorNote, Fab, SectionTitle, Segmented, SkeletonList, Spinner, cx, haptic, riseStyle } from '../../components/ui';
import { announce, useTopic } from '../../lib/bus';
import { flashRow, useFocusRequest } from '../../lib/focus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { usePullRefresh } from '../../lib/nav';
import {
  TYPE_ICON,
  sortCollections,
  type SavedAnswer,
  type SavedCollection,
  type SavedItem,
  type SavedListing,
  type SavedSearchResponse,
} from '../../lib/saved';
import CollectionsSheet, { type SavedFilter } from './CollectionsSheet';
import { CollectionDot, CollectionPicker, SavedRow } from './parts';
import SavedItemSheet, { moveItem } from './SavedItemSheet';
import SaveSheet from './SaveSheet';

const PAGE = 30;
const SEARCH_DEBOUNCE_MS = 400;

type Mode = 'find' | 'ask';

function filterQuery(f: SavedFilter): { collection?: string; type?: SavedItem['type'] } {
  if (f.kind === 'collection') return { collection: f.id };
  if (f.kind === 'none') return { collection: 'none' };
  if (f.kind === 'type') return { type: f.type };
  return {};
}

function sameFilter(a: SavedFilter, b: SavedFilter): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

const ASK_EXAMPLES = ['What did I save about productivity last month?', "C'est quoi l'app pour marcher que j'ai gardée ?", 'Chno savit had simana?'];
const FIND_EXAMPLES = ['the video about a nostalgic football song', "l'article sur le manager engineering", 'une app pour marcher'];

/** The Saved library: describe-to-find search, Ask, collections, the recently saved list, the "+" Save sheet. */
export default function SavedView() {
  const { client, notify } = useHub();
  const [mode, setMode] = useState<Mode>('find');
  const [q, setQ] = useState('');
  const [filter, setFilter] = useState<SavedFilter>({ kind: 'all' });
  const collections = useHubData((c, s) => c.savedCollections(s), []);
  const listing = useHubData((c, s) => c.savedList({ ...filterQuery(filter), limit: PAGE }, s), [JSON.stringify(filter)]);
  const [extra, setExtra] = useState<SavedItem[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [search, setSearch] = useState<{ loading: boolean; data?: SavedSearchResponse; error?: string }>({ loading: false });
  const [answer, setAnswer] = useState<{ loading: boolean; data?: SavedAnswer; error?: string; question?: string }>({ loading: false });
  const [detail, setDetail] = useState<{ item: SavedItem; open: boolean } | null>(null);
  const [moving, setMoving] = useState<SavedItem | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const [allOpen, setAllOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const cols = useMemo(() => sortCollections(collections.data?.collections ?? []), [collections.data]);
  const colById = useMemo(() => new Map<string, SavedCollection>(cols.map((c) => [c.id, c])), [cols]);
  const items = useMemo(() => {
    const seen = new Set<string>();
    return [...(listing.data?.items ?? []), ...extra].filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true)));
  }, [listing.data, extra]);

  const reload = () => {
    collections.reload();
    listing.reload();
    setExtra([]);
  };
  useTopic(['saved'], reload);
  usePullRefresh(reload);
  useEffect(() => {
    setExtra([]);
  }, [filter]);

  // Global search, the Home section or a share opened an item / a collection.
  useFocusRequest<{ filter?: SavedFilter; item?: SavedItem }>('saved', (req) => {
    if (req.data?.filter) {
      setFilter(req.data.filter);
      return true;
    }
    if (req.data?.item) {
      setDetail({ item: req.data.item, open: true });
      return true;
    }
    client
      .savedItem(req.id)
      .then((item) => {
        setDetail({ item, open: true });
        flashRow(item.id);
      })
      .catch((err) => notify(errorText(err), 'error'));
    return true;
  });

  const term = q.trim();
  useEffect(() => {
    if (mode !== 'find' || term.length < 2) {
      setSearch({ loading: false });
      return undefined;
    }
    const ctrl = new AbortController();
    setSearch((s) => ({ loading: true, data: s.data }));
    const t = window.setTimeout(() => {
      client
        .savedSearch(term, 20, ctrl.signal)
        .then((data) => setSearch({ loading: false, data }))
        .catch((err) => {
          if (!ctrl.signal.aborted) setSearch({ loading: false, error: errorText(err) });
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [term, mode, client]);

  const ask = async (e?: FormEvent) => {
    e?.preventDefault();
    if (term.length < 2 || answer.loading) return;
    haptic(8);
    inputRef.current?.blur();
    setAnswer({ loading: true, question: term });
    try {
      setAnswer({ loading: false, data: await client.savedAsk(term), question: term });
    } catch (err) {
      setAnswer({ loading: false, error: errorText(err), question: term });
    }
  };

  const loadMore = async () => {
    const offset = listing.data?.next_offset;
    if (offset == null || loadingMore) return;
    setLoadingMore(true);
    try {
      const more: SavedListing = await client.savedList({ ...filterQuery(filter), limit: PAGE, offset: offset + extra.length });
      setExtra((x) => [...x, ...more.items]);
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setLoadingMore(false);
    }
  };

  const replace = (item: SavedItem) => {
    listing.setData((l) => (l ? { ...l, items: l.items.map((i) => (i.id === item.id ? { ...i, ...item } : i)) } : l));
    setExtra((x) => x.map((i) => (i.id === item.id ? { ...i, ...item } : i)));
    setSearch((s) => (s.data ? { ...s, data: { ...s.data, results: s.data.results.map((r) => (r.item.id === item.id ? { ...r, item } : r)) } } : s));
    setDetail((d) => (d && d.item.id === item.id ? { ...d, item } : d));
  };

  const remove = async (item: SavedItem): Promise<boolean> => {
    try {
      await client.savedDelete(item.id);
      listing.setData((l) => (l ? { ...l, items: l.items.filter((i) => i.id !== item.id), total: l.total - 1 } : l));
      setExtra((x) => x.filter((i) => i.id !== item.id));
      setSearch((s) => (s.data ? { ...s, data: { ...s.data, results: s.data.results.filter((r) => r.item.id !== item.id) } } : s));
      setDetail((d) => (d && d.item.id === item.id ? { ...d, open: false } : d));
      collections.reload();
      announce('saved');
      notify('Deleted', 'ok');
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const move = async (item: SavedItem, target: { id: string } | { name: string } | { none: true }) => {
    setMoving(null);
    try {
      replace(await moveItem(client, item, target));
      collections.reload();
      notify('Moved', 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const open = (item: SavedItem) => {
    haptic(6);
    setDetail({ item, open: true });
  };

  const row = (item: SavedItem, i: number, extraNode?: ReactNode) => (
    <div key={item.id} className="rise" style={riseStyle(i)}>
      <SwipeRow
        start={{ icon: 'folder', label: 'Move', tone: 'signal', run: () => setMoving(item) }}
        end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => remove(item) }}
      >
        <SavedRow item={item} collection={item.collection_id ? colById.get(item.collection_id) : undefined} onOpen={() => open(item)} extra={extraNode} />
      </SwipeRow>
    </div>
  );

  const filterTitle =
    filter.kind === 'collection'
      ? colById.get(filter.id)?.name ?? 'Collection'
      : filter.kind === 'none'
        ? 'Uncategorised'
        : filter.kind === 'type'
          ? collections.data?.smart.find((s) => s.type === filter.type)?.label ?? 'Type'
          : 'Recently saved';
  const searching = mode === 'find' && term.length >= 2;
  const total = collections.data?.total ?? 0;

  return (
    <div className="space-y-5">
      <div className="space-y-2.5">
        <Segmented<Mode>
          value={mode}
          onChange={(m) => {
            setMode(m);
            window.setTimeout(() => inputRef.current?.focus(), 30);
          }}
          options={[
            { value: 'find', label: 'Find' },
            { value: 'ask', label: 'Ask' },
          ]}
        />
        <form onSubmit={(e) => (mode === 'ask' ? void ask(e) : e.preventDefault())} className="relative">
          <Icon name={mode === 'ask' ? 'spark' : 'search'} size={20} className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-400" />
          <input
            ref={inputRef}
            type="text"
            inputMode="search"
            enterKeyHint={mode === 'ask' ? 'send' : 'search'}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={mode === 'ask' ? 'Ask about your saved items…' : 'Describe what you saved…'}
            aria-label={mode === 'ask' ? 'Ask about your saved items' : 'Describe what you saved'}
            className="min-h-[56px] w-full rounded-[20px] bg-ink-850 pl-12 pr-12 text-[17px] text-ink-100 ring-1 ring-inset ring-ink-700 placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-signal/70"
          />
          {mode === 'ask' ? (
            <button type="submit" aria-label="Ask" disabled={term.length < 2 || answer.loading} className="press absolute right-1.5 top-1/2 grid h-11 w-11 -translate-y-1/2 place-items-center rounded-full bg-signal text-ink-950 disabled:opacity-40">
              {answer.loading ? <Spinner className="border-ink-950/30 border-t-ink-950" /> : <Icon name="send" size={18} />}
            </button>
          ) : (
            q && (
              <button type="button" aria-label="Clear" onClick={() => setQ('')} className="press absolute right-1.5 top-1/2 grid h-11 w-11 -translate-y-1/2 place-items-center rounded-full text-ink-400 hover:bg-ink-800">
                <Icon name="x" size={18} />
              </button>
            )
          )}
        </form>
      </div>

      {mode === 'ask' && (
        <section className="space-y-3" aria-live="polite">
          {!answer.data && !answer.loading && !answer.error && (
            <div className="space-y-2">
              <p className="px-1 text-[14px] text-ink-400">Answers come only from what you saved, with links to the items.</p>
              <div className="flex flex-wrap gap-2">
                {ASK_EXAMPLES.map((ex) => (
                  <Chip key={ex} onClick={() => setQ(ex)}>
                    {ex}
                  </Chip>
                ))}
              </div>
            </div>
          )}
          {answer.loading && (
            <div className="space-y-2 rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" aria-hidden="true">
              <div className="skeleton h-3.5 w-11/12" />
              <div className="skeleton h-3.5 w-4/5" />
              <div className="skeleton h-3.5 w-2/3" />
            </div>
          )}
          {answer.error && <ErrorNote message={answer.error} onRetry={() => void ask()} />}
          {answer.data && (
            <div className="space-y-3">
              <div className="rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
                <Markdown
                  text={answer.data.answer}
                  onCite={(n) => {
                    const src = answer.data?.sources.find((s) => s.n === n);
                    if (src) client.savedItem(src.id).then(open, (err) => notify(errorText(err), 'error'));
                  }}
                />
              </div>
              {answer.data.sources.length > 0 && (
                <div className="overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                  {answer.data.sources.map((s, i) => (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => client.savedItem(s.id).then(open, (err) => notify(errorText(err), 'error'))}
                      className={cx('press flex min-h-[56px] w-full items-center gap-3 px-3 py-2 text-left hover:bg-ink-850', i > 0 && 'border-t border-ink-800')}
                    >
                      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-signal/15 text-[12px] font-semibold text-signal">{s.n}</span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[14px] text-ink-100">{s.title}</span>
                        <span className="flex items-center gap-1 truncate text-[12px] text-ink-400">
                          <Icon name={TYPE_ICON[s.type] ?? 'bookmark'} size={12} />
                          {s.site || 'Saved'}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {searching && (
        <section aria-live="polite" className="space-y-2">
          {search.loading && !search.data && <SkeletonList rows={3} />}
          {search.error && <ErrorNote message={search.error} />}
          {search.data && search.data.results.length === 0 && !search.loading && (
            <EmptyState
              icon="search"
              title="Nothing matches yet"
              hint="Try other words, or switch to Ask to question your saved items."
              action={
                <Button variant="subtle" icon="spark" onClick={() => setMode('ask')}>
                  Ask instead
                </Button>
              }
            />
          )}
          {search.data && search.data.results.length > 0 && (
            <>
              <p className="flex items-center gap-2 px-1 text-[13px] text-ink-400">
                {search.loading && <Spinner />}
                {search.data.results.length} match{search.data.results.length === 1 ? '' : 'es'}
                {search.data.mode === 'keyword' && <Badge tone="warn">keywords only</Badge>}
              </p>
              <div className="space-y-2">
                {search.data.results.map((r, i) =>
                  row(
                    r.item,
                    i,
                    <span className="mt-1 block space-y-1">
                      <span className="flex flex-wrap gap-1">
                        <Badge tone={r.match === 'meaning' ? 'signal' : 'neutral'}>{r.reason}</Badge>
                      </span>
                      {r.snippet && <span className="line-clamp-2 block text-[12px] leading-snug text-ink-400">{r.snippet}</span>}
                    </span>,
                  ),
                )}
              </div>
            </>
          )}
        </section>
      )}

      {!searching && mode === 'find' && (
        <>
          {!q && total > 0 && (
            <div className="no-scrollbar -mx-4 -mt-2 flex gap-2 overflow-x-auto px-4">
              {FIND_EXAMPLES.map((ex) => (
                <button key={ex} type="button" onClick={() => setQ(ex)} className="press shrink-0 rounded-full px-3 py-1.5 text-[13px] text-ink-400 ring-1 ring-inset ring-ink-800">
                  “{ex}”
                </button>
              ))}
            </div>
          )}

          <section>
            <SectionTitle
              action={
                <button type="button" onClick={() => setAllOpen(true)} className="press min-h-[44px] px-2 text-[14px] text-signal">
                  See all
                </button>
              }
            >
              Browse by collection
            </SectionTitle>
            {collections.error && <ErrorNote message={collections.error} onRetry={collections.reload} />}
            <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4 pb-1">
              {collections.loading && !collections.data && [0, 1, 2].map((i) => <div key={i} className="skeleton h-9 w-28 shrink-0 rounded-full" />)}
              {collections.data && (
                <>
                  <Chip active={filter.kind === 'all'} onClick={() => setFilter({ kind: 'all' })}>
                    All <span className="opacity-60">{total}</span>
                  </Chip>
                  {cols.map((c) => (
                    <Chip key={c.id} active={sameFilter(filter, { kind: 'collection', id: c.id })} onClick={() => setFilter({ kind: 'collection', id: c.id })}>
                      <CollectionDot color={c.color} />
                      {c.emoji ? `${c.emoji} ` : ''}
                      {c.name} <span className="opacity-60">{c.count}</span>
                    </Chip>
                  ))}
                  {collections.data.uncategorised > 0 && cols.length > 0 && (
                    <Chip active={filter.kind === 'none'} onClick={() => setFilter({ kind: 'none' })}>
                      Uncategorised <span className="opacity-60">{collections.data.uncategorised}</span>
                    </Chip>
                  )}
                  {collections.data.smart.map((s) => (
                    <Chip key={s.type} active={sameFilter(filter, { kind: 'type', type: s.type })} onClick={() => setFilter({ kind: 'type', type: s.type })}>
                      <Icon name={TYPE_ICON[s.type] ?? 'bookmark'} size={14} />
                      {s.label} <span className="opacity-60">{s.count}</span>
                    </Chip>
                  ))}
                </>
              )}
            </div>
          </section>

          <section>
            <SectionTitle
              action={
                filter.kind !== 'all' ? (
                  <button type="button" onClick={() => setFilter({ kind: 'all' })} className="press min-h-[44px] px-2 text-[14px] text-signal">
                    Show all
                  </button>
                ) : undefined
              }
            >
              {filterTitle}
            </SectionTitle>
            {listing.error && <ErrorNote message={listing.error} onRetry={listing.reload} />}
            {listing.loading && !listing.data && <SkeletonList rows={5} />}
            {listing.data && items.length === 0 && (
              <EmptyState
                icon="bookmark"
                title={filter.kind === 'all' ? 'Save anything from any app' : 'Nothing here yet'}
                hint={
                  filter.kind === 'all'
                    ? 'Share a link, a post or a video to OmniFree from any app, or tap + to paste one. Forgot what it was called? Just describe it.'
                    : 'Items you file here show up in this list.'
                }
                action={
                  <Button variant="primary" icon="plus" onClick={() => setSaveOpen(true)}>
                    Save something
                  </Button>
                }
              />
            )}
            <div className="space-y-2">{items.map((item, i) => row(item, i))}</div>
            {listing.data?.next_offset != null && items.length < (listing.data?.total ?? 0) && (
              <Button variant="ghost" className="mt-2 w-full" onClick={() => void loadMore()} disabled={loadingMore}>
                {loadingMore ? 'Loading…' : 'Load more'}
              </Button>
            )}
          </section>
        </>
      )}

      <Fab icon="plus" label="Save a link, text or file" onClick={() => setSaveOpen(true)} />
      <SaveSheet
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        collections={cols}
        onSaved={(item) => {
          listing.setData((l) => (l ? { ...l, items: [item, ...l.items.filter((i) => i.id !== item.id)] } : l));
          collections.reload();
        }}
      />
      <CollectionsSheet open={allOpen} onClose={() => setAllOpen(false)} data={collections.data} onPick={setFilter} />
      {detail && (
        <SavedItemSheet
          item={detail.item}
          open={detail.open}
          collections={cols}
          onClose={() => setDetail((d) => (d ? { ...d, open: false } : d))}
          onChange={(item) => {
            replace(item);
            collections.reload();
          }}
          onDelete={(item) => void remove(item)}
        />
      )}
      {moving && (
        <CollectionPicker
          open={!!moving}
          onClose={() => setMoving(null)}
          collections={cols}
          current={moving.collection_id}
          suggestion={moving.suggestion?.name}
          onPick={(t) => void move(moving, t)}
        />
      )}
    </div>
  );
}
