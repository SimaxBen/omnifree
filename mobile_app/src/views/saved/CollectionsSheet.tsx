import { useState, type FormEvent } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Button, cx, haptic, inputClass } from '../../components/ui';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import { COLLECTION_COLORS, TYPE_ICON, type SavedCollection, type SavedCollections } from '../../lib/saved';
import { CollectionDot } from './parts';

export type SavedFilter = { kind: 'all' } | { kind: 'collection'; id: string } | { kind: 'none' } | { kind: 'type'; type: SmartType };
type SmartType = SavedCollections['smart'][number]['type'];

function ColorDots({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Color">
      {COLLECTION_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={value === c}
          aria-label={c}
          onClick={() => onChange(c)}
          className={cx('press h-8 w-8 rounded-full ring-2 ring-offset-2 ring-offset-ink-900', value === c ? 'ring-ink-100' : 'ring-transparent')}
          style={{ background: c }}
        />
      ))}
    </div>
  );
}

function Editor({ col, onDone }: { col: SavedCollection; onDone: () => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [name, setName] = useState(col.name);
  const [emoji, setEmoji] = useState(col.emoji ?? '');
  const [color, setColor] = useState(col.color ?? COLLECTION_COLORS[0]);
  const [busy, setBusy] = useState(false);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await client.savedPatchCollection(col.id, { name: name.trim(), emoji: emoji.trim(), color });
      announce('saved');
      onDone();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    const ok = await confirm({
      title: `Delete "${col.name}"?`,
      body: col.count ? `Its ${col.count} item${col.count === 1 ? '' : 's'} stay saved, as Uncategorised.` : undefined,
      confirmLabel: 'Delete collection',
      tone: 'danger',
    });
    if (!ok) return;
    try {
      await client.savedDeleteCollection(col.id);
      announce('saved');
      onDone();
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };
  return (
    <form onSubmit={save} className="space-y-3 rounded-row bg-ink-850 p-3 ring-1 ring-inset ring-ink-700">
      <div className="flex gap-2">
        <input value={emoji} onChange={(e) => setEmoji(e.target.value)} maxLength={4} placeholder="🙂" aria-label="Emoji" className={cx(inputClass, 'w-16 px-0 text-center')} />
        <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} aria-label="Name" className={inputClass} />
      </div>
      <ColorDots value={color} onChange={setColor} />
      <div className="flex gap-2">
        <Button variant="danger" icon="trash" onClick={() => void remove()} aria-label="Delete collection" />
        <Button variant="ghost" className="flex-1" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" className="flex-1" disabled={busy || !name.trim()}>
          Save
        </Button>
      </div>
    </form>
  );
}

/** "See all": every collection and smart collection with counts; edit, create, delete. */
export default function CollectionsSheet({
  open,
  onClose,
  data,
  onPick,
}: {
  open: boolean;
  onClose: () => void;
  data: SavedCollections | undefined;
  onPick: (f: SavedFilter) => void;
}) {
  const { client, notify } = useHub();
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [emoji, setEmoji] = useState('');
  const [color, setColor] = useState(COLLECTION_COLORS[1]);
  const [busy, setBusy] = useState(false);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await client.savedCreateCollection({ name: name.trim(), color, ...(emoji.trim() ? { emoji: emoji.trim() } : {}) });
      haptic(8);
      announce('saved');
      setName('');
      setEmoji('');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  const pick = (f: SavedFilter) => {
    haptic(6);
    onPick(f);
    onClose();
  };

  const rowCls = 'press flex min-h-[56px] min-w-0 flex-1 items-center gap-3 px-1 text-left text-[15px] text-ink-100';
  return (
    <Sheet open={open} onClose={onClose} full title="Collections">
      <div className="space-y-5 pb-6">
        <section className="divide-y divide-ink-800">
          <button type="button" className={cx(rowCls, 'w-full')} onClick={() => pick({ kind: 'all' })}>
            <Icon name="bookmark" size={18} className="text-signal" />
            <span className="flex-1">Everything</span>
            <span className="text-[13px] text-ink-500">{data?.total ?? ''}</span>
          </button>
          {(data?.collections ?? []).map((c) =>
            editing === c.id ? (
              <div key={c.id} className="py-2">
                <Editor col={c} onDone={() => setEditing(null)} />
              </div>
            ) : (
              <div key={c.id} className="flex items-center gap-1">
                <button type="button" className={rowCls} onClick={() => pick({ kind: 'collection', id: c.id })}>
                  <CollectionDot color={c.color} />
                  <span className="min-w-0 flex-1 truncate">
                    {c.emoji ? `${c.emoji} ` : ''}
                    {c.name}
                  </span>
                  <span className="text-[13px] text-ink-500">{c.count}</span>
                </button>
                <button type="button" aria-label={`Edit ${c.name}`} onClick={() => setEditing(c.id)} className="press grid h-11 w-11 shrink-0 place-items-center rounded-full text-ink-400 hover:bg-ink-800">
                  <Icon name="edit" size={18} />
                </button>
              </div>
            ),
          )}
          <button type="button" className={cx(rowCls, 'w-full')} onClick={() => pick({ kind: 'none' })}>
            <CollectionDot color={null} />
            <span className="flex-1 text-ink-300">Uncategorised</span>
            <span className="text-[13px] text-ink-500">{data?.uncategorised ?? ''}</span>
          </button>
        </section>

        {!!data?.smart.length && (
          <section>
            <h3 className="px-1 pb-1 text-[13px] text-ink-400">By type</h3>
            <div className="grid grid-cols-2 gap-2">
              {data.smart.map((s) => (
                <button
                  key={s.type}
                  type="button"
                  onClick={() => pick({ kind: 'type', type: s.type })}
                  className="press flex min-h-[52px] items-center gap-2 rounded-row bg-ink-850 px-3 text-left text-[14px] text-ink-100 ring-1 ring-inset ring-ink-700"
                >
                  <Icon name={TYPE_ICON[s.type] ?? 'bookmark'} size={17} className="text-ink-300" />
                  <span className="min-w-0 flex-1 truncate">{s.label}</span>
                  <span className="text-[12px] text-ink-500">{s.count}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        <form onSubmit={create} className="space-y-3">
          <h3 className="px-1 text-[13px] text-ink-400">New collection</h3>
          <div className="flex gap-2">
            <input value={emoji} onChange={(e) => setEmoji(e.target.value)} maxLength={4} placeholder="🙂" aria-label="Emoji" className={cx(inputClass, 'w-16 px-0 text-center')} />
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} placeholder="e.g. Productivity" aria-label="Collection name" className={inputClass} />
          </div>
          <ColorDots value={color} onChange={setColor} />
          <Button type="submit" variant="primary" className="w-full" disabled={busy || !name.trim()}>
            Create
          </Button>
        </form>
      </div>
    </Sheet>
  );
}
