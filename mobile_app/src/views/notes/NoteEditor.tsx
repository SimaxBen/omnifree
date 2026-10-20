import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Markdown } from '../../components/Markdown';
import { Sheet, useConfirm } from '../../components/Sheet';
import { IconButton, Segmented, cx, haptic, inputClass } from '../../components/ui';
import { announce } from '../../lib/bus';
import { formatDateTime } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { toggleCheckbox } from '../../lib/markdown';
import type { Note, NotePatch } from '../../lib/types';
import { TaskRow } from './TaskRow';

type SaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'error';

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

  const books = useHubData((c, s) => c.notes({ limit: 1 }, s), []);
  const linked = useHubData(async (c, s) => (id ? (await c.tasks({ status: 'all' }, s)).filter((t) => t.note_id === id) : []), [id]);

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
        setSave('saved');
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

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

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

  const status = { idle: '', pending: 'Editing', saving: 'Saving…', saved: 'Saved', error: 'Not saved' }[save];
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
          {status && <span className={cx('text-[13px]', save === 'error' ? 'text-danger-400' : save === 'saved' ? 'text-mint-400' : 'text-ink-500')}>{status}</span>}
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
          <textarea
            aria-label="Note body (Markdown)"
            value={draft.body}
            onChange={(e) => set('body', e.target.value)}
            placeholder={'Start writing. Markdown works: # heading, - list, - [ ] task, **bold**.'}
            className={cx(inputClass, 'min-h-[46dvh] resize-none py-3 leading-relaxed')}
            autoFocus={!note}
          />
        ) : draft.body.trim() ? (
          <Markdown text={draft.body} onToggle={(i) => set('body', toggleCheckbox(draft.body, i))} className="min-h-[30dvh] screen-in-up" />
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

        {updatedAt && <p className="px-1 text-[12px] text-ink-500">Last saved {formatDateTime(updatedAt)}</p>}
      </div>
    </Sheet>
  );
}
