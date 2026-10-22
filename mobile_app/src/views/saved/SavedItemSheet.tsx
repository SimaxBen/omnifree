import { useEffect, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, cx, haptic, inputClass } from '../../components/ui';
import { announce } from '../../lib/bus';
import { copyText } from '../../lib/dom';
import { errorText, useHub } from '../../lib/hub';
import { TYPE_ICON, TYPE_LABEL, savedDate, sourceLabel, type SavedCollection, type SavedItem } from '../../lib/saved';
import { CollectionDot, CollectionPicker, SavedThumb } from './parts';

function savedWhen(iso: string): string {
  const d = savedDate(iso);
  if (!d) return '';
  return d === 'Today' || d === 'Yesterday' ? `saved ${d.toLowerCase()}` : `saved on ${d}`;
}

/** Moves `item` to a picked collection (or creates one by name); resolves to the updated item. */
export async function moveItem(
  client: ReturnType<typeof useHub>['client'],
  item: SavedItem,
  target: { id: string } | { name: string } | { none: true },
): Promise<SavedItem> {
  const patch = 'id' in target ? { collection_id: target.id } : 'name' in target ? { collection: target.name } : { collection_id: '' };
  const updated = await client.savedPatch(item.id, patch);
  announce('saved');
  return updated;
}

/** Detail of one saved item: open the original, summary, tags, note, collection, copy, share, refresh, delete. */
export default function SavedItemSheet({
  item,
  open,
  collections,
  onClose,
  onChange,
  onDelete,
}: {
  item: SavedItem;
  open: boolean;
  collections: SavedCollection[];
  onClose: () => void;
  onChange: (item: SavedItem) => void;
  onDelete: (item: SavedItem) => void;
}) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [full, setFull] = useState<SavedItem>(item);
  const [note, setNote] = useState(item.note ?? '');
  const [busy, setBusy] = useState<'note' | 'refresh' | 'move' | null>(null);
  const [picking, setPicking] = useState(false);

  useEffect(() => {
    setFull(item);
    setNote(item.note ?? '');
    if (!open) return undefined;
    const ctrl = new AbortController();
    client
      .savedItem(item.id, ctrl.signal)
      .then((x) => {
        setFull(x);
      })
      .catch(() => undefined); // the list row is enough to show the sheet
    return () => ctrl.abort();
  }, [item, open, client]);

  const update = (x: SavedItem) => {
    setFull((prev) => ({ ...x, text: x.text ?? prev.text }));
    onChange(x);
  };

  const collection = collections.find((c) => c.id === full.collection_id);
  const link = full.url ?? '';
  const noteDirty = (full.note ?? '') !== note.trim();

  const saveNote = async () => {
    setBusy('note');
    try {
      update(await client.savedPatch(full.id, { note: note.trim() || null }));
      notify('Note saved', 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const move = async (target: { id: string } | { name: string } | { none: true }) => {
    setPicking(false);
    setBusy('move');
    try {
      const x = await moveItem(client, full, target);
      update(x);
      const name = collections.find((c) => c.id === x.collection_id)?.name ?? ('name' in target ? target.name : '');
      notify(x.collection_id ? `Moved to ${name || 'collection'}` : 'Moved to Uncategorised', 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const refresh = async () => {
    setBusy('refresh');
    try {
      const x = await client.savedRefresh(full.id);
      update(x);
      notify(x.enrich_status === 'pending' ? 'The site still did not answer' : 'Refreshed', x.enrich_status === 'pending' ? 'info' : 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const share = async () => {
    const data = { title: full.title, text: full.summary ?? undefined, url: link || undefined };
    try {
      if (typeof navigator !== 'undefined' && navigator.share) {
        await navigator.share(data);
        return;
      }
    } catch {
      return; // cancelled
    }
    const ok = await copyText(link || full.title);
    notify(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'error');
  };

  const remove = async () => {
    if (!(await confirm({ title: 'Delete this saved item?', body: full.title, confirmLabel: 'Delete', tone: 'danger' }))) return;
    onDelete(full);
  };

  return (
    <Sheet open={open} onClose={onClose} full title={<span className="line-clamp-1">{TYPE_LABEL[full.type] ?? 'Saved'}</span>}>
      <div className="space-y-5 pb-6">
        {full.thumb ? (
          <a href={link || undefined} target="_blank" rel="noopener noreferrer" className="block overflow-hidden rounded-card bg-ink-850 ring-1 ring-inset ring-ink-800">
            <img src={full.thumb} alt="" referrerPolicy="no-referrer" className="aspect-[16/9] w-full object-cover" onError={(e) => ((e.currentTarget.parentElement as HTMLElement).style.display = 'none')} />
          </a>
        ) : null}

        <div className="flex items-start gap-3">
          {!full.thumb && <SavedThumb item={full} size={52} />}
          <div className="min-w-0 flex-1">
            <h2 className="font-display text-[20px] font-semibold leading-snug text-ink-100">{full.title}</h2>
            <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-[13px] text-ink-400">
              <Icon name={TYPE_ICON[full.type] ?? 'bookmark'} size={14} className="text-ink-500" />
              {[sourceLabel(full), full.author, savedWhen(full.saved_at)].filter(Boolean).join(' · ')}
            </p>
          </div>
        </div>

        <div className="grid grid-cols-4 gap-2">
          {link ? (
            <a href={link} target="_blank" rel="noopener noreferrer" className="press flex min-h-[64px] flex-col items-center justify-center gap-1 rounded-row bg-signal text-[12px] font-semibold text-ink-950">
              <Icon name="external" size={20} />
              Open
            </a>
          ) : (
            <span className="flex min-h-[64px] flex-col items-center justify-center gap-1 rounded-row bg-ink-850 text-[12px] text-ink-500">
              <Icon name={full.kind === 'file' ? 'file' : 'note'} size={20} />
              {full.kind === 'file' ? 'Drive' : 'Text'}
            </span>
          )}
          {[
            { icon: 'copy' as const, label: 'Copy', run: () => void copyText(link || full.text || full.title).then((ok) => notify(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'error')) },
            { icon: 'share' as const, label: 'Share', run: () => void share() },
            { icon: 'refresh' as const, label: busy === 'refresh' ? 'Reading…' : 'Refresh', run: () => void refresh() },
          ].map((a) => (
            <button
              key={a.label}
              type="button"
              onClick={() => {
                haptic(6);
                a.run();
              }}
              disabled={!!busy}
              className="press flex min-h-[64px] flex-col items-center justify-center gap-1 rounded-row bg-ink-850 text-[12px] text-ink-200 ring-1 ring-inset ring-ink-700 disabled:opacity-50"
            >
              <Icon name={a.icon} size={20} className={cx(a.icon === 'refresh' && busy === 'refresh' && 'spin')} />
              {a.label}
            </button>
          ))}
        </div>

        {full.enrich_status === 'pending' && (
          <div className="flex items-start gap-3 rounded-row bg-amber-400/10 px-4 py-3 text-[14px] text-amber-400">
            <Icon name="clock" size={18} className="mt-0.5 shrink-0" />
            <p className="min-w-0 flex-1">The page could not be read yet{full.enrich_error ? ` (${full.enrich_error})` : ''}. The link is saved; Refresh tries again.</p>
          </div>
        )}

        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-[13px] text-ink-400">Collection</span>
            <button
              type="button"
              onClick={() => setPicking(true)}
              disabled={busy === 'move'}
              className="press ml-auto inline-flex min-h-[40px] items-center gap-2 rounded-full bg-ink-850 px-3.5 text-[14px] text-ink-100 ring-1 ring-inset ring-ink-700"
            >
              <CollectionDot color={collection?.color} />
              {collection ? `${collection.emoji ? `${collection.emoji} ` : ''}${collection.name}` : 'Uncategorised'}
              <Icon name="down" size={14} className="text-ink-400" />
            </button>
          </div>
          {full.suggestion && !full.collection_id && (
            <button
              type="button"
              onClick={() => void move({ name: full.suggestion!.name })}
              disabled={!!busy}
              className="press flex min-h-[48px] w-full items-center gap-2 rounded-row bg-signal/10 px-3.5 text-left text-[14px] text-signal ring-1 ring-inset ring-signal/30"
            >
              <Icon name="spark" size={16} />
              <span className="min-w-0 flex-1 truncate">
                Add to <b>{full.suggestion.name}</b>
                {full.suggestion.new ? ' (new collection)' : ''}
              </span>
              <span className="text-[12px] font-semibold">Accept</span>
            </button>
          )}
        </section>

        {(full.summary || full.description) && (
          <section className="space-y-1.5">
            <h3 className="text-[13px] text-ink-400">Summary{full.ai_source === 'heuristic' ? ' (from the page)' : ''}</h3>
            <p className="text-[15px] leading-relaxed text-ink-200">{full.summary || full.description}</p>
          </section>
        )}

        {full.tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {full.tags.map((t) => (
              <Badge key={t}>#{t}</Badge>
            ))}
          </div>
        )}

        <section className="space-y-2">
          <label htmlFor={`note-${full.id}`} className="block text-[13px] text-ink-400">
            Your note
          </label>
          <textarea
            id={`note-${full.id}`}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            maxLength={4000}
            placeholder="Why you saved it, what to remember…"
            className={cx(inputClass, 'min-h-[88px] py-3 leading-snug')}
          />
          {noteDirty && (
            <Button variant="primary" onClick={() => void saveNote()} disabled={busy === 'note'} className="w-full">
              {busy === 'note' ? 'Saving…' : 'Save note'}
            </Button>
          )}
        </section>

        {full.text && (
          <details className="rounded-row bg-ink-850 px-4 py-3 text-[14px] text-ink-300">
            <summary className="cursor-pointer select-none text-ink-200">Text from the page</summary>
            <p className="mt-2 whitespace-pre-line break-words leading-relaxed">{full.text}</p>
          </details>
        )}

        {link && <p className="break-all font-mono text-[12px] text-ink-500">{link}</p>}

        <Button variant="danger" icon="trash" onClick={() => void remove()} className="w-full">
          Delete
        </Button>
      </div>
      <CollectionPicker
        open={picking}
        onClose={() => setPicking(false)}
        collections={collections}
        current={full.collection_id}
        suggestion={full.suggestion?.name}
        onPick={(t) => void move(t)}
      />
    </Sheet>
  );
}
