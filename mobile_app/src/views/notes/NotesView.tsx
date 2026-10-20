import { useEffect, useMemo, useState } from 'react';
import { Icon } from '../../components/icons';
import { SwipeRow } from '../../components/SwipeRow';
import { Button, Chip, EmptyState, ErrorNote, Segmented, SkeletonList, cx, inputClass, riseStyle , Fab } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { announce, useTopic } from '../../lib/bus';
import { relativeTime } from '../../lib/files';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { plainText } from '../../lib/markdown';
import { usePullRefresh, useNav } from '../../lib/nav';
import type { Note } from '../../lib/types';
import TasksPanel from './TasksPanel';

function NoteRow({ note, onOpen }: { note: Note; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} className="flex w-full gap-3 px-4 py-3.5 text-left active:bg-ink-850">
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          {note.pinned && <Icon name="pin" size={14} className="shrink-0 text-signal" />}
          {note.source === 'voice' && <Icon name="mic" size={14} className="shrink-0 text-danger-400" />}
          <span className="truncate font-display text-[16px] font-semibold text-ink-100">{note.title || 'Untitled'}</span>
        </span>
        <span className="mt-0.5 line-clamp-2 block text-[14px] leading-snug text-ink-400">{plainText(note.body, 180) || 'Empty note'}</span>
        <span className="mt-1.5 flex flex-wrap items-center gap-x-2 text-[12px] text-ink-500">
          <span>{relativeTime(note.updated_at)}</span>
          <span>{note.notebook}</span>
          {note.tags.slice(0, 3).map((t) => (
            <span key={t} className="text-signal/80">
              #{t}
            </span>
          ))}
        </span>
      </span>
    </button>
  );
}

function NotesPanel() {
  const { client, notify } = useHub();
  const actions = useActions();
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [notebook, setNotebook] = useState('');

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q]);

  const list = useHubData((c, s) => c.notes({ notebook: notebook || undefined, q: debounced || undefined, limit: 100 }, s), [notebook, debounced]);
  useTopic(['notes'], list.reload);
  usePullRefresh(list.reload);

  const notes = list.data?.notes ?? [];
  const pinned = useMemo(() => notes.filter((n) => n.pinned), [notes]);
  const rest = useMemo(() => notes.filter((n) => !n.pinned), [notes]);
  const total = list.data?.notebooks.reduce((s, b) => s + b.count, 0) ?? 0;

  const togglePin = async (n: Note) => {
    list.setData((d) => d && { ...d, notes: d.notes.map((x) => (x.id === n.id ? { ...x, pinned: !x.pinned } : x)) });
    try {
      await client.updateNote(n.id, { pinned: !n.pinned });
      list.reload();
    } catch (err) {
      notify(errorText(err), 'error');
      list.reload();
    }
  };

  const remove = async (n: Note) => {
    try {
      await client.deleteNote(n.id);
      list.setData((d) => d && { ...d, notes: d.notes.filter((x) => x.id !== n.id) });
      announce('notes');
      notify(`Deleted ${n.title || 'note'}`, 'ok');
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const group = (title: string | null, items: Note[], offset: number) =>
    items.length > 0 && (
      <section className="space-y-2">
        {title && <h2 className="px-1 font-display text-[15px] font-semibold text-ink-400">{title}</h2>}
        <div className="space-y-2">
          {items.map((n, i) => (
            <div key={n.id} className="rise" style={riseStyle(offset + i)}>
              <SwipeRow
                start={{ icon: 'pin', label: n.pinned ? 'Unpin' : 'Pin', tone: 'signal', run: () => void togglePin(n) }}
                end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => remove(n) }}
              >
                <NoteRow note={n} onOpen={() => actions.openNote(n)} />
              </SwipeRow>
            </div>
          ))}
        </div>
      </section>
    );

  return (
    <div className="space-y-4">
      <div className="relative">
        <Icon name="search" size={18} className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-500" />
        <input type="search" aria-label="Search notes" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search titles, text and tags" className={cx(inputClass, 'pl-11')} />
      </div>

      <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4">
        <Chip active={!notebook} onClick={() => setNotebook('')}>
          All <span className="opacity-60">{total || ''}</span>
        </Chip>
        {list.data?.notebooks.map((b) => (
          <Chip key={b.name} active={notebook === b.name} onClick={() => setNotebook(b.name)}>
            {b.name} <span className="opacity-60">{b.count}</span>
          </Chip>
        ))}
      </div>

      {list.error && <ErrorNote message={list.error} onRetry={list.reload} />}
      {list.loading && !list.data && <SkeletonList rows={5} />}
      {list.data && notes.length === 0 && (
        <EmptyState
          icon="note"
          title={debounced ? 'No notes match' : notebook ? `${notebook} is empty` : 'No notes yet'}
          hint={debounced ? 'Try another word, or search a tag without the #.' : 'Write one, or record a voice note and let it be written for you.'}
          action={
            !debounced && (
              <Button variant="primary" icon="plus" onClick={() => actions.newNote({ notebook: notebook || undefined })}>
                New note
              </Button>
            )
          }
        />
      )}
      {group(pinned.length && rest.length ? 'Pinned' : null, pinned, 0)}
      {group(pinned.length && rest.length ? 'Notes' : null, rest, pinned.length)}
      {notes.length > 0 && <p className="px-1 text-center text-[12px] text-ink-500">Swipe right to pin, left to delete.</p>}

      {notes.length > 0 && (
        <Fab icon="edit" label="New note" onClick={() => actions.newNote({ notebook: notebook || undefined })} />
      )}
    </div>
  );
}

export default function NotesView() {
  const { route, setSub } = useNav();
  const section = route.sub === 'tasks' ? 'tasks' : 'notes';
  return (
    <div className="space-y-4">
      <Segmented
        value={section}
        onChange={(v) => setSub(v === 'notes' ? '' : v)}
        options={[
          { value: 'notes', label: 'Notes' },
          { value: 'tasks', label: 'Tasks' },
        ]}
      />
      <div key={section} className="screen-in-up">
        {section === 'notes' ? <NotesPanel /> : <TasksPanel />}
      </div>
    </div>
  );
}
