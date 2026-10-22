import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Markdown } from '../../components/Markdown';
import { Sheet, useConfirm } from '../../components/Sheet';
import { IconButton, Segmented, cx, haptic, inputClass } from '../../components/ui';
import { announce } from '../../lib/bus';
import { formatDateTime } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { completeWikiLink, toggleCheckbox, wikiQuery } from '../../lib/markdown';
import { isPending } from '../../lib/offline/model';
import type { Note, NotePatch } from '../../lib/types';
import { useActions } from '../../lib/actions';
import { useNav } from '../../lib/nav';
import { noteBacklinks, noteTitles, openNoteLink, requestTagFilter, type TitleHit } from '../../lib/noteLinks';
import { TaskRow } from './TaskRow';

type SaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'queued' | 'error';

interface Draft {
  title: string;
  body: string;
  notebook: string;
  tags: string[];
  pinned: boolean;
}

function draftOf(note: Note | null, seed?: { notebook?: string; body?: string }): Draft {
  return {
    title: note?.title ?? '',
    body: note?.body ?? seed?.body ?? '',
    notebook: note?.notebook ?? seed?.notebook ?? 'Inbox',
    tags: note?.tags ?? [],
    pinned: note?.pinned ?? false,
  };
}

function diff(a: Draft, b: Draft): NotePatch {
  const p: NotePatch = {};
  if (a.title !== b.title) p.title = b.title;
  if (a.body !== b.body) p.body = b.body;
  if (a.notebook !== b.notebook) p.notebook = b.notebook;
  if (a.tags.join('\u0000') !== b.tags.join('\u0000')) p.tags = b.tags;
  if (a.pinned !== b.pinned) p.pinned = b.pinned;
  return p;
}

/** Voice-note audio: the key has no provider, so locate it first, then presign. */
function VoiceAudio({ audioKey }: { audioKey: string }) {
  const { client } = useHub();
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        const obj = await client.driveLocate(audioKey, ctrl.signal);
        const res = await client.drivePresign(obj.key || audioKey, obj.backend, ctrl.signal);
        if (!ctrl.signal.aborted) setUrl(res.url);
      } catch (err) {
        if (!ctrl.signal.aborted) setError(errorText(err));
      }
    })();
    return () => ctrl.abort();
  }, [client, audioKey]);
  return (
    <div className="flex items-center gap-3 rounded-row bg-ink-850 p-3">
      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-danger-400/15 text-danger-400">
        <Icon name="wave" size={20} />
      </span>
      {url ? <audio src={url} controls preload="metadata" className="h-10 min-w-0 flex-1" /> : <p className="text-[14px] text-ink-400">{error ? `Audio unavailable: ${error}` : 'Loading recording…'}</p>}
    </div>
  );
}

export default function NoteEditor({
  open,
  note,
  seed,
  onClose,
}: {
  open: boolean;
  note: Note | null;
  seed?: { notebook?: string; body?: string };
  onClose: () => void;
}) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [id, setId] = useState<string | null>(note?.id ?? null);
  const [draft, setDraft] = useState<Draft>(() => draftOf(note, seed));
  const [mode, setMode] = useState<'write' | 'read'>(note && note.body ? 'read' : 'write');
  const [save, setSave] = useState<SaveState>('idle');
  const [tagInput, setTagInput] = useState('');
  const [updatedAt, setUpdatedAt] = useState(note?.updated_at ?? null);
  const saved = useRef<Draft>(draftOf(note, seed));
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const idRef = useRef(id);
  idRef.current = id;
  const inflight = useRef<Promise<void> | null>(null);
  const changed = useRef(false);
  const actions = useActions();
  const { go } = useNav();
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const [wq, setWq] = useState<{ start: number; query: string } | null>(null);
  const [hits, setHits] = useState<TitleHit[]>([]);

  const books = useHubData((c, s) => c.notes({ limit: 1 }, s), []);
  const linked = useHubData(async (c, s) => (id ? (await c.tasks({ status: 'all' }, s)).filter((t) => t.note_id === id) : []), [id]);
  // Backlinks follow the saves (the hub re-indexes [[links]] each time the body is saved).
  const links = useHubData(async (c, s) => (id ? noteBacklinks(c, id, s) : null), [id, updatedAt]);

  const flush = useCallback(async () => {
    if (inflight.current) await inflight.current;
    const cur = draftRef.current;
    const patch = diff(saved.current, cur);
    if (!Object.keys(patch).length) return;
    if (!idRef.current && !cur.body.trim() && !cur.title.trim()) return;
    setSave('saving');
    const run = (async () => {
      try {
        let res: Note;
        if (!idRef.current) {
          res = await client.createNote({ body: cur.body, title: cur.title.trim() || undefined, notebook: cur.notebook, tags: cur.tags, pinned: cur.pinned });
          setId(res.id);
          idRef.current = res.id;
          // The hub derives a title from the body when none was given.
          if (!cur.title.trim() && res.title) setDraft((d) => (d.title ? d : { ...d, title: res.title }));
          saved.current = { ...cur, title: cur.title.trim() ? cur.title : res.title };
        } else {
          res = await client.updateNote(idRef.current, patch);
          saved.current = cur;
        }
        changed.current = true;
        setUpdatedAt(res.updated_at);
        // Answered by the offline queue: kept on the phone, not on the hub yet.
        setSave(isPending(res) ? 'queued' : 'saved');
      } catch (err) {
        setSave('error');
        notify(`Not saved: ${errorText(err)}`, 'error');
      }
    })();
    inflight.current = run;
    await run;
    inflight.current = null;
  }, [client, notify]);

  // Autosave 800 ms after the last keystroke.
  useEffect(() => {
    if (!Object.keys(diff(saved.current, draft)).length) return undefined;
    setSave('pending');
    const t = window.setTimeout(() => void flush(), 800);
    return () => window.clearTimeout(t);
  }, [draft, flush]);

  const close = async () => {
    await flush();
    if (changed.current) announce('notes');
    onClose();
  };

  // Tapping a #tag shows the notes with that tag.
  const openTag = async (tag: string) => {
    haptic(8);
    await close();
    go('notes');
    requestTagFilter(tag);
  };

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

  // [[ autocomplete: titles matching what is typed after an open [[.
  useEffect(() => {
    if (!wq) {
      setHits([]);
      return undefined;
    }
    const ctrl = new AbortController();
    const t = window.setTimeout(() => {
      noteTitles(client, wq.query, ctrl.signal)
        .then((h) => setHits(h.filter((x) => x.id !== idRef.current && x.title.trim() !== '')))
        .catch(() => setHits([]));
    }, 150);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [client, wq]);

  const trackCaret = (el: HTMLTextAreaElement) => {
    const q = el.selectionStart === el.selectionEnd ? wikiQuery(el.value, el.selectionStart) : null;
    setWq((cur) => (cur?.start === q?.start && cur?.query === q?.query ? cur : q));
  };

  const pickLink = (title: string) => {
    const el = bodyRef.current;
    if (!el || !wq) return;
    const next = completeWikiLink(el.value, wq.start, el.selectionStart, title);
    set('body', next.text);
    setWq(null);
    window.requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(next.caret, next.caret);
    });
  };

  // Tapping a [[link]] opens that note, creating it when no note has that title yet.
  const openLink = async (title: string) => {
    haptic(8);
    await flush();
    try {
      const res = await openNoteLink(client, title, draftRef.current.notebook);
      if (res.created) notify(`Created “${res.note.title}”`, 'ok');
      if (changed.current || res.created) announce('notes');
      actions.openNote(res.note);
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  // Backlinks open by id: a linking note may be untitled, or share its title with another note.
  const openById = async (noteId: string) => {
    haptic(8);
    await flush();
    try {
      const note = await client.note(noteId);
      if (changed.current) announce('notes');
      actions.openNote(note);
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const addTag = () => {
    const t = tagInput.trim().replace(/^#/, '').toLowerCase();
    if (t && !draft.tags.includes(t)) set('tags', [...draft.tags, t]);
    setTagInput('');
  };

  const remove = async () => {
    if (!id) {
      onClose();
      return;
    }
    if (!(await confirm({ title: 'Delete this note?', body: 'The note is removed for good. Tasks made from it stay.', confirmLabel: 'Delete note', tone: 'danger' }))) return;
    try {
      await client.deleteNote(id);
      announce('notes');
      notify('Note deleted', 'ok');
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const status = { idle: '', pending: 'Editing', saving: 'Saving…', saved: 'Saved', queued: 'Saved on phone · pending sync', error: 'Not saved' }[save];
  const notebooks = books.data?.notebooks.map((b) => b.name) ?? [];

  return (
    <Sheet
      open={open}
      onClose={() => void close()}
      full
      title={
        <span className="flex items-center gap-2 text-[15px] font-normal text-ink-400">
          <Icon name="book" size={16} />
          <span className="truncate">{draft.notebook}</span>
          {status && <span className={cx('text-[13px]', save === 'error' ? 'text-danger-400' : save === 'saved' ? 'text-mint-400' : save === 'queued' ? 'text-amber-400' : 'text-ink-500')}>{status}</span>}
        </span>
      }
      headerAction={
        <div className="flex items-center">
          <IconButton
            icon="pin"
            label={draft.pinned ? 'Unpin note' : 'Pin note'}
            onClick={() => {
              haptic(8);
              set('pinned', !draft.pinned);
            }}
            className={draft.pinned ? 'text-signal' : ''}
          />
          <IconButton icon="trash" label="Delete note" onClick={() => void remove()} className="hover:text-danger-400" />
          <IconButton icon="check" label="Done" onClick={() => void close()} className="bg-ink-800 text-ink-100" />
        </div>
      }
    >
      <div className="space-y-4">
        <input
          aria-label="Title"
          value={draft.title}
          onChange={(e) => set('title', e.target.value)}
          placeholder="Title"
          className="w-full bg-transparent font-display text-[26px] font-semibold leading-tight text-ink-100 placeholder:text-ink-600 focus:outline-none"
        />

        <div className="no-scrollbar -mx-5 flex items-center gap-2 overflow-x-auto px-5">
          <label className="relative shrink-0">
            <span className="sr-only">Notebook</span>
            <input
              list="notebooks"
              value={draft.notebook}
              onChange={(e) => set('notebook', e.target.value)}
              onBlur={(e) => set('notebook', e.target.value.trim() || 'Inbox')}
              className="min-h-[36px] w-32 rounded-full bg-ink-850 px-3 text-[14px] text-ink-200 ring-1 ring-inset ring-ink-700 focus:outline-none focus:ring-signal/70"
            />
            <datalist id="notebooks">
              {notebooks.map((n) => (
                <option key={n} value={n} />
              ))}
            </datalist>
          </label>
          {draft.tags.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => set('tags', draft.tags.filter((x) => x !== t))}
              className="press pop inline-flex min-h-[36px] shrink-0 items-center gap-1 rounded-full bg-signal/15 px-3 text-[14px] text-signal"
              aria-label={`Remove tag ${t}`}
            >
              #{t}
              <Icon name="x" size={14} />
            </button>
          ))}
          <input
            aria-label="Add tag"
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ',' || e.key === ' ') {
                e.preventDefault();
                addTag();
              }
            }}
            onBlur={addTag}
            placeholder="+ tag"
            className="min-h-[36px] w-24 shrink-0 rounded-full bg-transparent px-3 text-[14px] text-ink-200 ring-1 ring-inset ring-dashed ring-ink-600 placeholder:text-ink-500 focus:outline-none"
          />
        </div>

        {note?.source === 'voice' && note.audio_key && <VoiceAudio audioKey={note.audio_key} />}

        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { value: 'write', label: 'Write' },
            { value: 'read', label: 'Preview' },
          ]}
        />

        {mode === 'write' ? (
          <div className="relative">
            {wq && (
              <div role="listbox" aria-label="Link to a note" className="no-scrollbar sticky top-0 z-10 -mx-1 mb-2 flex gap-2 overflow-x-auto rounded-row bg-ink-900/95 p-1.5 ring-1 ring-inset ring-ink-700 backdrop-blur">
                {hits.map((h) => (
                  <button
                    key={h.id}
                    type="button"
                    role="option"
                    aria-selected={false}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => pickLink(h.title)}
                    className="press flex min-h-[36px] max-w-[70%] shrink-0 items-center gap-1.5 rounded-full bg-ink-800 px-3 text-[14px] text-ink-100"
                  >
                    <Icon name="link" size={14} className="shrink-0 text-signal" />
                    <span className="truncate">{h.title}</span>
                  </button>
                ))}
                {wq.query.trim() && !hits.some((h) => h.title.toLowerCase() === wq.query.trim().toLowerCase()) && (
                  <button
                    type="button"
                    role="option"
                    aria-selected={false}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => pickLink(wq.query)}
                    className="press flex min-h-[36px] max-w-[70%] shrink-0 items-center gap-1.5 rounded-full px-3 text-[14px] text-signal ring-1 ring-inset ring-dashed ring-signal/50"
                  >
                    <Icon name="plus" size={14} className="shrink-0" />
                    <span className="truncate">New “{wq.query.trim()}”</span>
                  </button>
                )}
                {!hits.length && !wq.query.trim() && <span className="px-2 py-2 text-[13px] text-ink-500">Type a note title…</span>}
              </div>
            )}
            <textarea
              ref={bodyRef}
              aria-label="Note body (Markdown)"
              value={draft.body}
              onChange={(e) => {
                set('body', e.target.value);
                trackCaret(e.target);
              }}
              onSelect={(e) => trackCaret(e.currentTarget)}
              onBlur={() => window.setTimeout(() => setWq(null), 150)}
              placeholder={'Start writing. Markdown works: # heading, - list, - [ ] task, **bold**, [[Another note]], #tag.'}
              className={cx(inputClass, 'min-h-[46dvh] resize-none py-3 leading-relaxed')}
              autoFocus={!note}
            />
          </div>
        ) : draft.body.trim() ? (
          <Markdown
            text={draft.body}
            onToggle={(i) => set('body', toggleCheckbox(draft.body, i))}
            onWikiLink={(t) => void openLink(t)}
            onTag={(t) => void openTag(t)}
            className="min-h-[30dvh] screen-in-up"
          />
        ) : (
          <p className="py-10 text-center text-[15px] text-ink-500">Nothing written yet.</p>
        )}

        {(linked.data?.length ?? 0) > 0 && (
          <div className="space-y-2">
            <h3 className="px-1 font-display text-[16px] font-semibold text-ink-100">Tasks from this note</h3>
            {linked.data!.map((t) => (
              <TaskRow key={t.id} task={t} onChange={(nt) => linked.setData((ts) => ts?.map((x) => (x.id === nt.id ? nt : x)))} />
            ))}
          </div>
        )}

        {(links.data?.backlinks.length ?? 0) > 0 && (
          <div className="space-y-2">
            <h3 className="flex items-center gap-2 px-1 font-display text-[16px] font-semibold text-ink-100">
              <Icon name="link" size={16} className="text-signal" />
              Linked from {links.data!.backlinks.length} note{links.data!.backlinks.length === 1 ? '' : 's'}
            </h3>
            <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
              {links.data!.backlinks.map((b) => (
                <button key={b.id} type="button" onClick={() => void openById(b.id)} className="press block w-full px-4 py-3 text-left active:bg-ink-800">
                  <span className="flex items-baseline justify-between gap-2">
                    <span className={cx('truncate font-display text-[15px] font-semibold', b.title ? 'text-ink-100' : 'text-ink-400')}>{b.title || 'Untitled'}</span>
                    <span className="shrink-0 text-[12px] text-ink-500">{b.notebook}</span>
                  </span>
                  <span className="mt-0.5 line-clamp-2 block text-[13px] leading-snug text-ink-400">{b.snippet}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {(links.data?.outgoing.length ?? 0) > 0 && (
          <div className="flex flex-wrap items-center gap-2 px-1">
            <span className="text-[13px] text-ink-500">Links to</span>
            {links.data!.outgoing.map((o) => (
              <button
                key={o.title}
                type="button"
                onClick={() => void openLink(o.title)}
                className={cx(
                  'press inline-flex min-h-[32px] items-center gap-1 rounded-full px-3 text-[13px]',
                  o.note_id ? 'bg-ink-800 text-ink-100' : 'text-signal ring-1 ring-inset ring-dashed ring-signal/50',
                )}
                aria-label={o.note_id ? `Open ${o.title}` : `Create ${o.title}`}
              >
                {!o.note_id && <Icon name="plus" size={12} />}
                {o.title}
              </button>
            ))}
          </div>
        )}

        {updatedAt && <p className="px-1 text-[12px] text-ink-500">Last saved {formatDateTime(updatedAt)}</p>}
      </div>
    </Sheet>
  );
}
