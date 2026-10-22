import { useState, type FormEvent, type ReactNode } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Button, cx, haptic, inputClass } from '../../components/ui';
import { TYPE_ICON, TYPE_LABEL, siteDateLine, type SavedCollection, type SavedItem } from '../../lib/saved';

/** The item's thumbnail (cached copy or OG image), its type icon when there is none or it fails to load. */
export function SavedThumb({ item, size = 56, className }: { item: Pick<SavedItem, 'thumb' | 'type'>; size?: number; className?: string }) {
  const [failed, setFailed] = useState(false);
  const src = !failed && item.thumb ? item.thumb : null;
  return (
    <span
      className={cx('relative grid shrink-0 place-items-center overflow-hidden rounded-[14px] bg-ink-850 ring-1 ring-inset ring-ink-700', className)}
      style={{ width: size, height: size }}
    >
      {src ? (
        <img src={src} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)} className="h-full w-full object-cover" />
      ) : (
        <Icon name={TYPE_ICON[item.type] ?? 'bookmark'} size={Math.round(size * 0.4)} className="text-ink-400" />
      )}
    </span>
  );
}

/**
 * A tappable example question. Unlike `Chip` (one line, never shrinks) it wraps: a long example such as
 * "C'est quoi l'app pour marcher que j'ai gardée ?" is wider than a 375 px screen and pushed the page sideways.
 */
export function ExampleChip({ text, onPick }: { text: string; onPick: (text: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => {
        haptic(5);
        onPick(text);
      }}
      className="press inline-flex min-h-[36px] max-w-full min-w-0 items-center rounded-[18px] bg-ink-850 px-3.5 py-1.5 text-left text-[14px] leading-snug text-ink-300 ring-1 ring-inset ring-ink-700 [overflow-wrap:anywhere] hover:text-ink-100"
    >
      {text}
    </button>
  );
}

export function CollectionDot({ color, className }: { color: string | null | undefined; className?: string }) {
  return <span aria-hidden="true" className={cx('inline-block h-2.5 w-2.5 shrink-0 rounded-full', className)} style={{ background: color || '#5A6197' }} />;
}

/** One saved item: thumbnail, title, "site · date" with the type icon, and the collection when it has one. */
export function SavedRow({
  item,
  collection,
  onOpen,
  extra,
}: {
  item: SavedItem;
  collection?: SavedCollection;
  onOpen: () => void;
  extra?: ReactNode;
}) {
  return (
    <button type="button" onClick={onOpen} data-focus={item.id} className="press flex min-h-[72px] w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-ink-850 active:bg-ink-850">
      <SavedThumb item={item} />
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 text-[15px] font-medium leading-snug text-ink-100">{item.title}</span>
        <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[12px] text-ink-400">
          <Icon name={TYPE_ICON[item.type] ?? 'bookmark'} size={13} className="shrink-0 text-ink-500" aria-label={TYPE_LABEL[item.type]} />
          <span className="truncate">{siteDateLine(item)}</span>
          {collection && (
            <span className="flex min-w-0 max-w-[45%] shrink items-center gap-1 truncate text-ink-500">
              <CollectionDot color={collection.color} className="h-2 w-2" />
              <span className="truncate">{collection.name}</span>
            </span>
          )}
          {item.enrich_status === 'pending' && <Icon name="clock" size={12} className="shrink-0 text-amber-400" aria-label="Page not read yet" />}
        </span>
        {extra}
      </span>
    </button>
  );
}

/** Move an item: every collection, Uncategorised, or a new collection typed here. */
export function CollectionPicker({
  open,
  onClose,
  collections,
  current,
  suggestion,
  onPick,
}: {
  open: boolean;
  onClose: () => void;
  collections: SavedCollection[];
  current: string | null;
  suggestion?: string | null;
  onPick: (target: { id: string } | { name: string } | { none: true }) => void;
}) {
  const [name, setName] = useState('');
  const create = (e: FormEvent) => {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    haptic(8);
    onPick({ name: n });
    setName('');
  };
  const row = (key: string, label: ReactNode, active: boolean, run: () => void) => (
    <button
      key={key}
      type="button"
      onClick={() => {
        haptic(6);
        run();
      }}
      className={cx('press flex min-h-[52px] w-full items-center gap-3 rounded-row px-3 text-left text-[15px]', active ? 'bg-signal/15 text-signal' : 'text-ink-100 hover:bg-ink-850')}
    >
      {label}
      {active && <Icon name="check" size={18} className="ml-auto" />}
    </button>
  );
  return (
    <Sheet open={open} onClose={onClose} title="Move to collection">
      <div className="space-y-1 pb-2">
        {suggestion && !collections.some((c) => c.name.toLowerCase() === suggestion.toLowerCase()) &&
          row('suggested', (
            <>
              <Icon name="spark" size={18} className="text-signal" />
              <span className="flex-1">
                New: {suggestion} <span className="text-[12px] text-ink-400">(suggested)</span>
              </span>
            </>
          ), false, () => onPick({ name: suggestion }))}
        {collections.map((c) =>
          row(c.id, (
            <>
              <CollectionDot color={c.color} />
              <span className="min-w-0 flex-1 truncate">
                {c.emoji ? `${c.emoji} ` : ''}
                {c.name}
              </span>
              <span className="text-[12px] text-ink-500">{c.count}</span>
            </>
          ), current === c.id, () => onPick({ id: c.id })),
        )}
        {row('none', (
          <>
            <CollectionDot color={null} />
            <span className="flex-1 text-ink-300">Uncategorised</span>
          </>
        ), !current, () => onPick({ none: true }))}
        <form onSubmit={create} className="flex gap-2 pt-3">
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} placeholder="New collection" aria-label="New collection name" className={inputClass} />
          <Button type="submit" variant="primary" disabled={!name.trim()}>
            Add
          </Button>
        </form>
      </div>
    </Sheet>
  );
}
