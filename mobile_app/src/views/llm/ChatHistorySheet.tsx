import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { SwipeRow } from '../../components/SwipeRow';
import { Badge, Button, EmptyState, ErrorNote, IconButton, SkeletonList, cx, inputClass } from '../../components/ui';
import { dropPending, groupChatsByDate, loadPending, matchesChatQuery, summaryOf } from '../../lib/chats';
import { formatTime } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import type { ChatSummary } from '../../lib/types';

function when(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const days = (Date.now() - d.getTime()) / 86_400_000;
  if (days < 1) return formatTime(iso);
  return d.toLocaleDateString('en-GB', days < 7 ? { weekday: 'short' } : { day: 'numeric', month: 'short' });
}

/**
 * Past conversations: search, grouped by day, tap to reopen, swipe right to pin and left to delete,
 * or the row's menu to rename / pin / delete. Conversations still waiting to sync are listed too.
 */
export default function ChatHistorySheet({
  open,
  onClose,
  currentId,
  version,
  onOpen,
  onNew,
  onChanged,
  onDeleted,
}: {
  open: boolean;
  onClose: () => void;
  currentId: string;
  /** Bumped by the chat after each save so an open sheet refreshes. */
  version: number;
  onOpen: (id: string) => void;
  onNew: () => void;
  onChanged: (chat: ChatSummary) => void;
  onDeleted: (id: string) => void;
}) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [query, setQuery] = useState('');
  const [q, setQ] = useState('');
  const [menu, setMenu] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  const [pendingTick, setPendingTick] = useState(0);

  useEffect(() => {
    const t = window.setTimeout(() => setQ(query.trim()), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  const chats = useHubData((c, s) => (open ? c.chatList({ q, limit: 200 }, s) : Promise.resolve(undefined)), [open, q, version]);

  const list = useMemo(() => {
    const server = chats.data ?? [];
    const ids = new Set(server.map((c) => c.id));
    const pending = Object.values(loadPending())
      .map(summaryOf)
      .filter((c) => matchesChatQuery(c, q));
    // A queued version is newer than the hub's copy.
    const merged = [...pending, ...server.filter((c) => !pending.some((p) => p.id === c.id))];
    return { items: merged, unsynced: new Set(pending.map((p) => p.id)), onServer: ids };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chats.data, q, pendingTick, open]);

  const groups = groupChatsByDate(list.items);

  const replace = (chat: ChatSummary) => chats.setData((prev) => prev?.map((c) => (c.id === chat.id ? { ...c, ...chat } : c)));

  const togglePin = async (chat: ChatSummary) => {
    try {
      const updated = await client.chatUpdate(chat.id, { pinned: !chat.pinned });
      replace(updated);
      onChanged(updated);
    } catch (err) {
      notify(list.onServer.has(chat.id) ? errorText(err) : 'Not synced yet: pin it once the hub has it', 'error');
    }
  };

  const remove = async (chat: ChatSummary): Promise<boolean> => {
    const ok = await confirm({ title: 'Delete this conversation?', body: chat.title, confirmLabel: 'Delete', tone: 'danger' });
    if (!ok) return false;
    try {
      if (list.onServer.has(chat.id)) await client.chatDelete(chat.id);
      dropPending(chat.id);
      setPendingTick((t) => t + 1);
      chats.setData((prev) => prev?.filter((c) => c.id !== chat.id));
      onDeleted(chat.id);
      setMenu(null);
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const rename = async (e: FormEvent) => {
    e.preventDefault();
    if (!renaming) return;
    try {
      const updated = await client.chatUpdate(renaming.id, { title: renaming.title.trim() });
      replace(updated);
      onChanged(updated);
      setRenaming(null);
      setMenu(null);
    } catch (err) {
      notify(list.onServer.has(renaming.id) ? errorText(err) : 'Not synced yet: rename it once the hub has it', 'error');
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="History"
      full
      headerAction={
        <Button variant="subtle" icon="plus" className="min-h-[40px] px-3 text-[14px]" onClick={onNew}>
          New chat
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="relative">
          <Icon name="search" size={18} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-500" />
          <input
            type="search"
            aria-label="Search conversations"
            placeholder="Search conversations"
            className={cx(inputClass, 'pl-10')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        {chats.error && <ErrorNote message={`History unavailable: ${chats.error}`} onRetry={chats.reload} />}
        {chats.loading && !chats.data && <SkeletonList rows={4} />}
        {!chats.loading && !list.items.length && (
          <EmptyState
            icon="chat"
            title={q ? 'No matching conversation' : 'No saved conversations'}
            hint={q ? undefined : 'Each conversation is saved after every reply and syncs across your devices.'}
          />
        )}

        {groups.map((g) => (
          <section key={g.label} aria-label={g.label} className="space-y-1.5">
            <h3 className="px-1 text-[12px] font-semibold uppercase tracking-wider text-ink-500">{g.label}</h3>
            {g.chats.map((c) => (
              <div key={c.id}>
                <SwipeRow
                  start={{ icon: 'pin', label: c.pinned ? 'Unpin' : 'Pin', tone: 'signal', run: () => void togglePin(c) }}
                  end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => remove(c) }}
                >
                  <div className={cx('flex items-center gap-1 pr-1', c.id === currentId && 'bg-ink-850')}>
                    <button type="button" onClick={() => onOpen(c.id)} className="flex min-h-[64px] min-w-0 flex-1 flex-col justify-center px-3 py-2 text-left">
                      <span className="flex items-center gap-1.5">
                        {c.pinned && <Icon name="pin" size={13} className="shrink-0 text-signal" />}
                        <span className={cx('truncate text-[15px]', c.id === currentId ? 'font-semibold text-signal' : 'text-ink-100')}>{c.title}</span>
                      </span>
                      <span className="flex items-center gap-2 text-[12px] text-ink-500">
                        <span className="shrink-0 font-mono">{when(c.updated_at)}</span>
                        <span className="shrink-0">{c.message_count} msg</span>
                        {c.preview && <span className="truncate">{c.preview}</span>}
                      </span>
                    </button>
                    {list.unsynced.has(c.id) && <Badge tone="warn">not synced</Badge>}
                    <IconButton
                      icon="more"
                      label={`Actions for ${c.title}`}
                      aria-expanded={menu === c.id}
                      onClick={() => {
                        setMenu((m) => (m === c.id ? null : c.id));
                        setRenaming(null);
                      }}
                    />
                  </div>
                </SwipeRow>
                {menu === c.id && (
                  <div className="screen-in-up mt-1.5 rounded-row bg-ink-850 p-2">
                    {renaming?.id === c.id ? (
                      <form onSubmit={rename} className="flex gap-2">
                        <input
                          aria-label="Conversation title"
                          className={cx(inputClass, 'min-h-[42px] flex-1 bg-ink-900')}
                          value={renaming.title}
                          maxLength={300}
                          autoFocus
                          onChange={(e) => setRenaming({ id: c.id, title: e.target.value })}
                          placeholder="Empty = automatic title"
                        />
                        <Button type="submit" variant="primary" className="min-h-[42px]">
                          Save
                        </Button>
                      </form>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        <Button variant="ghost" icon="edit" className="min-h-[40px]" onClick={() => setRenaming({ id: c.id, title: c.title })}>
                          Rename
                        </Button>
                        <Button variant="ghost" icon="pin" className="min-h-[40px]" onClick={() => void togglePin(c)}>
                          {c.pinned ? 'Unpin' : 'Pin'}
                        </Button>
                        <Button variant="danger" icon="trash" className="min-h-[40px]" onClick={() => void remove(c)}>
                          Delete
                        </Button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </section>
        ))}
        {list.items.length > 0 && <p className="text-center text-[12px] text-ink-500">Swipe right to pin, left to delete.</p>}
      </div>
    </Sheet>
  );
}
