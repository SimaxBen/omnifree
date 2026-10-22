import { useMemo } from 'react';
import { Icon } from '../../components/icons';
import { Chip, SectionTitle, riseStyle } from '../../components/ui';
import { useTopic } from '../../lib/bus';
import { requestFocus } from '../../lib/focus';
import { useHubData } from '../../lib/hub';
import { useNav, usePullRefresh } from '../../lib/nav';
import { sortCollections } from '../../lib/saved';
import type { SavedFilter } from './CollectionsSheet';
import { CollectionDot, SavedRow } from './parts';

/** Home: "Browse by collection" chips and the last few saved items, each opening the Saved screen. */
export default function RecentlySaved() {
  const { go } = useNav();
  const recent = useHubData((c, s) => c.savedList({ limit: 4 }, s), []);
  const collections = useHubData((c, s) => c.savedCollections(s), []);
  useTopic(['saved'], () => {
    recent.reload();
    collections.reload();
  });
  usePullRefresh(() => {
    recent.reload();
    collections.reload();
  });
  const cols = useMemo(() => sortCollections(collections.data?.collections ?? []).filter((c) => c.count > 0).slice(0, 8), [collections.data]);

  const openFilter = (filter: SavedFilter) => {
    requestFocus({ kind: 'saved', id: '', data: { filter } });
    go('more', 'saved');
  };

  // An older hub without /saved, or a transient error: Home stays clean.
  if (recent.error || (!recent.data && !recent.loading)) return null;
  const items = recent.data?.items ?? [];

  return (
    <section aria-label="Recently saved">
      <SectionTitle
        action={
          <button type="button" onClick={() => go('more', 'saved')} className="press min-h-[44px] px-2 text-[14px] text-signal">
            See all
          </button>
        }
      >
        Recently saved
      </SectionTitle>
      {cols.length > 0 && (
        <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4 pb-2">
          {cols.map((c) => (
            <Chip key={c.id} onClick={() => openFilter({ kind: 'collection', id: c.id })}>
              <CollectionDot color={c.color} />
              {c.emoji ? `${c.emoji} ` : ''}
              {c.name} <span className="opacity-60">{c.count}</span>
            </Chip>
          ))}
        </div>
      )}
      {recent.loading && !recent.data && <div className="skeleton h-[72px] rounded-row" />}
      {recent.data && items.length === 0 && (
        <button type="button" onClick={() => go('more', 'saved')} className="press flex w-full items-center gap-3 rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800">
          <span className="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-signal/15 text-signal">
            <Icon name="bookmark" size={20} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block font-display text-[16px] font-semibold text-ink-100">Save anything from any app</span>
            <span className="block text-[13px] text-ink-400">Share a link to OmniFree. Forgot what it was called? Just describe it.</span>
          </span>
        </button>
      )}
      {items.length > 0 && (
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {items.map((item, i) => (
            <div key={item.id} className="rise" style={riseStyle(i)}>
              <SavedRow
                item={item}
                onOpen={() => {
                  requestFocus({ kind: 'saved', id: item.id, data: { item } });
                  go('more', 'saved');
                }}
              />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
