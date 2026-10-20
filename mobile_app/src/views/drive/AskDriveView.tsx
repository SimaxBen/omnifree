import { useMemo, useRef, useState, type FormEvent } from 'react';
import { FileName, FileTile } from '../../components/files';
import { Icon } from '../../components/icons';
import { Markdown } from '../../components/Markdown';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, EmptyState, ErrorNote, IconButton, SectionTitle, SkeletonList, Spinner, cx, haptic, riseStyle } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { announce, useTopic } from '../../lib/bus';
import { fileKind, isIndexable, relativeTime } from '../../lib/files';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { usePullRefresh } from '../../lib/nav';
import type { RagAnswer, RagDoc, RagSource, StoredObject } from '../../lib/types';

type IndexState = 'queued' | 'indexing' | 'done' | 'error';

function asObject(provider: string, key: string, size?: number | null): StoredObject {
  return { key, backend: provider, size: size ?? 0 };
}

/** Pick files to index; they are indexed one per request (the hub has ~60 s per call). */
function IndexSheet({ open, onClose, docs }: { open: boolean; onClose: () => void; docs: RagDoc[] }) {
  const { client } = useHub();
  const files = useHubData((c, s) => c.driveList('', '', s), []);
  const indexed = useMemo(() => new Set(docs.filter((d) => !d.error).map((d) => `${d.provider}/${d.key}`)), [docs]);
  const candidates = useMemo(
    () =>
      (files.data?.objects ?? [])
        .filter((o) => isIndexable(o.key))
        .sort((a, b) => Number(indexed.has(`${a.backend}/${a.key}`)) - Number(indexed.has(`${b.backend}/${b.key}`)) || a.key.localeCompare(b.key)),
    [files.data, indexed],
  );
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [progress, setProgress] = useState<Record<string, { state: IndexState; note?: string }>>({});
  const [running, setRunning] = useState(false);
  const stop = useRef(false);

  const run = async () => {
    setRunning(true);
    stop.current = false;
    const queue = candidates.filter((o) => picked.has(`${o.backend}/${o.key}`));
    setProgress(Object.fromEntries(queue.map((o) => [`${o.backend}/${o.key}`, { state: 'queued' as IndexState }])));
    for (const o of queue) {
      if (stop.current) break;
      const k = `${o.backend}/${o.key}`;
      setProgress((p) => ({ ...p, [k]: { state: 'indexing' } }));
      try {
        const doc = await client.ragIndex(o.backend, o.key);
        setProgress((p) => ({ ...p, [k]: doc.error ? { state: 'error', note: doc.error } : { state: 'done', note: `${doc.chunks} passages` } }));
      } catch (err) {
        setProgress((p) => ({ ...p, [k]: { state: 'error', note: errorText(err) } }));
      }
      announce('rag');
    }
    setRunning(false);
    haptic([10, 40, 10]);
  };

  const done = Object.values(progress).filter((p) => p.state === 'done' || p.state === 'error').length;
  const total = Object.keys(progress).length;

  return (
    <Sheet
      open={open}
      onClose={() => {
        stop.current = true;
        onClose();
      }}
      full
      title="Index files"
      footer={
        running ? (
          <Button className="w-full" onClick={() => (stop.current = true)}>
            Stop after this file ({done} of {total})
          </Button>
        ) : (
          <Button variant="primary" icon="spark" className="w-full" disabled={!picked.size} onClick={() => void run()}>
            Index {picked.size || ''} file{picked.size === 1 ? '' : 's'}
          </Button>
        )
      }
    >
      <p className="pb-3 text-[14px] text-ink-400">PDF, text, Markdown, CSV, JSON, HTML and Word files. Re-indexing replaces the old passages.</p>
      {total > 0 && (
        <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-ink-800">
          <div className="h-full rounded-full bg-signal transition-[width] duration-slow ease-out" style={{ width: `${(done / total) * 100}%` }} />
        </div>
      )}
      {files.loading && !files.data && <SkeletonList rows={5} />}
      {files.data && !candidates.length && <EmptyState icon="file" title="No files Ask my Drive can read" hint="Upload PDFs, notes or documents first." />}
      <ul className="-mx-5 divide-y divide-ink-800">
        {candidates.map((o) => {
          const k = `${o.backend}/${o.key}`;
          const p = progress[k];
          const on = picked.has(k);
          return (
            <li key={k}>
              <button
                type="button"
                disabled={running}
                onClick={() =>
                  setPicked((s) => {
                    const n = new Set(s);
                    if (n.has(k)) n.delete(k);
                    else n.add(k);
                    return n;
                  })
                }
                className="flex min-h-[60px] w-full items-center gap-3 px-5 py-2 text-left active:bg-ink-850"
                aria-pressed={on}
              >
                <span className={cx('grid h-6 w-6 shrink-0 place-items-center rounded-md ring-2 ring-inset transition-colors duration-fast', on ? 'bg-signal text-ink-950 ring-signal' : 'ring-ink-500')}>
                  {on && <Icon name="check" size={14} strokeWidth={3} />}
                </span>
                <FileTile kind={fileKind(o.key)} size={36} />
                <span className="min-w-0 flex-1">
                  <FileName objKey={o.key} />
                  <span className={cx('block truncate text-[12px]', p?.state === 'error' ? 'text-danger-400' : p?.state === 'done' ? 'text-mint-400' : 'text-ink-500')}>
                    {p ? (p.state === 'queued' ? 'Waiting' : p.state === 'indexing' ? 'Reading and embedding…' : p.note) : indexed.has(k) ? 'Already indexed' : o.key}
                  </span>
                </span>
                {p?.state === 'indexing' && <Spinner />}
                {p?.state === 'done' && <Icon name="check" size={18} className="pop text-mint-400" />}
              </button>
            </li>
          );
        })}
      </ul>
    </Sheet>
  );
}

function SourceCard({ s, onOpen }: { s: RagSource; onOpen: () => void }) {
  return (
    <div className="screen-in-up rounded-row bg-ink-850 p-3 ring-1 ring-inset ring-signal/30">
      <div className="flex items-start gap-2">
        <span className="grid h-6 min-w-[24px] place-items-center rounded-full bg-signal px-1.5 text-[12px] font-semibold text-ink-950">{s.n}</span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[15px] font-semibold text-ink-100">{s.title}</p>
          <p className="text-[12px] text-ink-500">
            {s.page !== null ? `Page ${s.page}, ` : ''}relevance {Math.round(s.score * 100)}%
          </p>
        </div>
        <IconButton icon="external" label={`Open ${s.title}`} onClick={onOpen} className="-m-2" />
      </div>
      <p className="mt-2 border-l-2 border-ink-600 pl-3 text-[14px] leading-relaxed text-ink-300">{s.snippet}</p>
    </div>
  );
}

export default function AskDriveView() {
  const { client, notify } = useHub();
  const actions = useActions();
  const confirm = useConfirm();
  const docs = useHubData((c, s) => c.ragDocs(s), []);
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState<(RagAnswer & { question: string }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cite, setCite] = useState<number | null>(null);
  const [indexing, setIndexing] = useState<{ id: number; open: boolean } | null>(null);
  const [busyDoc, setBusyDoc] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const sheetId = useRef(1);

  useTopic(['rag'], docs.reload);
  usePullRefresh(docs.reload);

  const ask = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = question.trim();
    if (!q || asking) return;
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setAsking(true);
    setError(null);
    setCite(null);
    try {
      const res = await client.ragAsk({ question: q, top_k: 8 }, ctrl.signal);
      setAnswer({ ...res, question: q });
      haptic(8);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) setError(errorText(err));
    } finally {
      setAsking(false);
    }
  };

  const reindex = async (d: RagDoc) => {
    const k = `${d.provider}/${d.key}`;
    setBusyDoc(k);
    try {
      const doc = await client.ragIndex(d.provider, d.key);
      docs.setData((cur) => cur && { ...cur, docs: cur.docs.map((x) => (x.provider === d.provider && x.key === d.key ? doc : x)) });
      notify(doc.error ? `Not indexed: ${doc.error}` : `Re-indexed ${doc.title}`, doc.error ? 'error' : 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusyDoc(null);
    }
  };

  const remove = async (d: RagDoc) => {
    if (!(await confirm({ title: 'Remove from Ask my Drive?', body: `${d.title} stays in your Drive; only its index is removed.`, confirmLabel: 'Remove index', tone: 'danger' }))) return;
    try {
      await client.ragDelete(d.provider, d.key);
    } catch (err) {
      // 404: it was never indexed (or already removed); either way it is gone.
      if (!(err instanceof Error && 'status' in err && (err as { status: number }).status === 404)) {
        notify(errorText(err), 'error');
        return;
      }
    }
    docs.setData((cur) => cur && { ...cur, docs: cur.docs.filter((x) => !(x.provider === d.provider && x.key === d.key)) });
  };

  const activeSource = answer?.sources.find((s) => s.n === cite);
  const list = docs.data?.docs ?? [];

  return (
    <div className="space-y-6">
      <form onSubmit={(e) => void ask(e)} className="rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
        <textarea
          aria-label="Question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void ask();
            }
          }}
          rows={2}
          placeholder="Ask your documents anything: “When does my lease end?”"
          className="w-full resize-none bg-transparent px-2 py-1 text-[16px] text-ink-100 placeholder:text-ink-500 focus:outline-none"
        />
        <div className="flex items-center justify-between gap-2 pt-1">
          <span className="px-2 text-[12px] text-ink-500">{list.length ? `${list.filter((d) => !d.error).length} documents indexed` : 'Index files first'}</span>
          <Button variant="primary" icon="spark" type="submit" disabled={!question.trim() || asking}>
            {asking ? 'Thinking…' : 'Ask'}
          </Button>
        </div>
      </form>

      {error && <ErrorNote message={error} onRetry={() => void ask()} />}

      {asking && (
        <div className="space-y-2 rounded-card bg-ink-900 p-4" aria-hidden="true">
          <div className="skeleton h-4 w-11/12" />
          <div className="skeleton h-4 w-9/12" />
          <div className="skeleton h-4 w-10/12" />
        </div>
      )}

      {answer && !asking && (
        <section className="screen-in-up space-y-3">
          <p className="px-1 text-[13px] text-ink-500">{answer.question}</p>
          <div className="rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
            <Markdown text={answer.answer} onCite={(n) => setCite((c) => (c === n ? null : n))} activeCite={cite} />
            {answer.routed_via && <p className="pt-3 text-[12px] text-ink-500">Answered by {answer.routed_via}</p>}
          </div>
          {activeSource && <SourceCard key={activeSource.n} s={activeSource} onOpen={() => actions.preview(asObject(activeSource.provider, activeSource.key))} />}
          {answer.sources.length > 0 && (
            <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4">
              {answer.sources.map((s) => (
                <button
                  key={s.n}
                  type="button"
                  onClick={() => setCite((c) => (c === s.n ? null : s.n))}
                  className={cx('press flex min-h-[40px] max-w-[220px] shrink-0 items-center gap-2 rounded-full px-3 text-[13px] ring-1 ring-inset', cite === s.n ? 'bg-signal/15 text-signal ring-signal/40' : 'bg-ink-850 text-ink-300 ring-ink-700')}
                >
                  <span className="font-semibold">{s.n}</span>
                  <span className="truncate">{s.title}</span>
                  {s.page !== null && <span className="text-ink-500">p.{s.page}</span>}
                </button>
              ))}
            </div>
          )}
        </section>
      )}

      <section>
        <SectionTitle
          action={
            <Button icon="plus" onClick={() => setIndexing({ id: sheetId.current++, open: true })} className="min-h-[40px]">
              Index files
            </Button>
          }
        >
          Indexed documents
        </SectionTitle>
        {docs.data?.embedding_model && <p className="-mt-1 px-1 pb-2 text-[12px] text-ink-500">Embeddings: {docs.data.embedding_model}</p>}
        {docs.error && <ErrorNote message={docs.error} onRetry={docs.reload} />}
        {docs.loading && !docs.data && <SkeletonList rows={3} />}
        {docs.data && !list.length && <EmptyState icon="spark" title="Nothing indexed yet" hint="Index a few PDFs or notes and ask questions across them, with sources." />}
        <ul className="space-y-2">
          {list.map((d, i) => {
            const k = `${d.provider}/${d.key}`;
            return (
              <li key={k} className="rise flex items-center gap-2 rounded-row bg-ink-900 py-2 pl-3 ring-1 ring-inset ring-ink-800" style={riseStyle(i)}>
                <button type="button" onClick={() => actions.preview(asObject(d.provider, d.key, d.size))} className="flex min-w-0 flex-1 items-center gap-3 text-left">
                  <FileTile kind={fileKind(d.key, d.mime)} size={40} />
                  <span className="min-w-0 flex-1">
                    <FileName objKey={d.key} />
                    <span className={cx('block truncate text-[12px]', d.error ? 'text-danger-400' : 'text-ink-500')}>
                      {d.error ? d.error : `${d.chunks} passages, ${relativeTime(d.indexed_at)}`}
                    </span>
                  </span>
                </button>
                {busyDoc === k ? (
                  <Spinner className="mx-3" />
                ) : (
                  <>
                    {d.error && <Badge tone="bad">failed</Badge>}
                    <IconButton icon="refresh" label={`Re-index ${d.title}`} onClick={() => void reindex(d)} />
                    <IconButton icon="trash" label={`Remove ${d.title} from the index`} onClick={() => void remove(d)} className="hover:text-danger-400" />
                  </>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {indexing && <IndexSheet key={indexing.id} open={indexing.open} onClose={() => setIndexing((s) => (s ? { ...s, open: false } : s))} docs={list} />}
    </div>
  );
}
