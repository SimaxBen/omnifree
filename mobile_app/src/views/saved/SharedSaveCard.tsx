import { useState } from 'react';
import { Icon } from '../../components/icons';
import { Button, Card, Chip, haptic } from '../../components/ui';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { TYPE_ICON, siteDateLine, sortCollections, type SaveResult, type SavedItem } from '../../lib/saved';
import { CollectionDot, SavedThumb } from './parts';
import { moveItem } from './SavedItemSheet';

/** After "Save" on the share screen: what was saved, the suggested collection (one tap) and the optional picker. */
export default function SharedSaveCard({
  result,
  onChange,
  onOpen,
  onDone,
}: {
  result: SaveResult;
  onChange: (item: SavedItem) => void;
  onOpen: () => void;
  onDone: () => void;
}) {
  const { client, notify } = useHub();
  const collections = useHubData((c, s) => c.savedCollections(s), []);
  const [busy, setBusy] = useState(false);
  const item = result.item;
  const cols = sortCollections(collections.data?.collections ?? []);

  const move = async (target: { id: string } | { name: string }) => {
    if (busy) return;
    haptic(8);
    setBusy(true);
    try {
      const updated = await moveItem(client, item, target);
      onChange(updated);
      const name = 'name' in target ? target.name : cols.find((c) => c.id === target.id)?.name;
      notify(`Added to ${name ?? 'collection'}`, 'ok');
      collections.reload();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="space-y-3 p-4">
      <div className="flex items-start gap-3">
        <SavedThumb item={item} size={60} />
        <div className="min-w-0 flex-1">
          <p className="flex items-center gap-1.5 text-[12px] font-semibold text-mint-400">
            <Icon name="check" size={14} />
            {result.duplicate ? 'Already in your library' : 'Saved'}
          </p>
          <p className="line-clamp-2 font-display text-[16px] font-semibold leading-snug text-ink-100">{item.title}</p>
          <p className="flex items-center gap-1.5 truncate text-[12px] text-ink-400">
            <Icon name={TYPE_ICON[item.type] ?? 'bookmark'} size={12} />
            {siteDateLine(item)}
          </p>
        </div>
      </div>
      {item.summary && <p className="line-clamp-3 text-[13px] leading-snug text-ink-300">{item.summary}</p>}

      {item.suggestion && !item.collection_id && (
        <button
          type="button"
          onClick={() => void move({ name: item.suggestion!.name })}
          disabled={busy}
          className="press flex min-h-[48px] w-full items-center gap-2 rounded-row bg-signal/10 px-3.5 text-left text-[14px] text-signal ring-1 ring-inset ring-signal/30"
        >
          <Icon name="spark" size={16} />
          <span className="min-w-0 flex-1 truncate">
            Add to <b>{item.suggestion.name}</b>
            {item.suggestion.new ? ' (new)' : ''}
          </span>
          <span className="text-[12px] font-semibold">Accept</span>
        </button>
      )}

      {cols.length > 0 && (
        <div>
          <p className="pb-1.5 text-[12px] text-ink-500">{item.collection_id ? 'Collection' : 'Or pick a collection'}</p>
          <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4">
            {cols.map((c) => (
              <Chip key={c.id} active={item.collection_id === c.id} onClick={() => item.collection_id !== c.id && void move({ id: c.id })}>
                <CollectionDot color={c.color} />
                {c.emoji ? `${c.emoji} ` : ''}
                {c.name}
              </Chip>
            ))}
          </div>
        </div>
      )}

      <div className="flex gap-2 pt-1">
        <Button variant="subtle" className="flex-1" icon="bookmark" onClick={onOpen}>
          Open Saved
        </Button>
        <Button variant="primary" className="flex-1" onClick={onDone}>
          Done
        </Button>
      </div>
    </Card>
  );
}
