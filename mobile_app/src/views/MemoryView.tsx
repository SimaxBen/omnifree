import { useEffect, useState, type FormEvent } from 'react';
import { Icon } from '../components/icons';
import { useConfirm } from '../components/Sheet';
import { Badge, Button, EmptyState, ErrorNote, IconButton, SkeletonList, cx, haptic, inputClass, riseStyle } from '../components/ui';
import { type MemoryFact, memoryCreate, memoryDelete, memoryForgetAll, memoryList, memoryUpdate } from '../lib/assistant';
import { relativeTime } from '../lib/files';
import { errorText, useHub, useHubData } from '../lib/hub';
import { usePullRefresh } from '../lib/nav';

function FactRow({ fact, onSaved, onDeleted }: { fact: MemoryFact; onSaved: (f: MemoryFact) => void; onDeleted: (id: string) => void }) {
  const { client, notify } = useHub();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(fact.fact);
  const [busy, setBusy] = useState(false);
  // Delete takes two taps (the button sits next to the text a thumb taps to edit): the first arms it for 3 s.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), 3000);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const remove = () => {
    if (!armed) {
      haptic(5);
      setArmed(true);
      return;
    }
    setArmed(false);
    void run(async () => {
      await memoryDelete(client, fact.id);
      haptic(10);
      onDeleted(fact.id);
      notify(`Forgot: ${fact.fact}`, 'ok');
    });
  };

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    run(async () => {
      const t = text.trim();
      if (!t || t === fact.fact) {
        setEditing(false);
        return;
      }
      onSaved(await memoryUpdate(client, fact.id, { fact: t }));
      setEditing(false);
    });

  return (
    <div className="flex items-start gap-2 px-3 py-2.5">
      <button
        type="button"
        onClick={() => void run(async () => onSaved(await memoryUpdate(client, fact.id, { pinned: !fact.pinned })))}
        disabled={busy}
        aria-pressed={fact.pinned}
        aria-label={fact.pinned ? 'Unpin: use only when relevant' : 'Pin: always use this fact with your data'}
        className={cx('press mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full', fact.pinned ? 'bg-signal/15 text-signal' : 'text-ink-500')}
      >
        <Icon name="pin" size={16} />
      </button>
      <div className="min-w-0 flex-1">
        {editing ? (
          <textarea
            className={cx(inputClass, 'min-h-[64px] py-2')}
            value={text}
            maxLength={300}
            autoFocus
            onChange={(e) => setText(e.target.value)}
            aria-label="Fact"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void save();
              }
            }}
          />
        ) : (
          <button type="button" className="block w-full text-left text-[15px] text-ink-100" onClick={() => setEditing(true)}>
            {fact.fact}
          </button>
        )}
        <p className="pt-0.5 text-[12px] text-ink-500">
          {fact.kind === 'preference' ? 'Answer preference, every chat · ' : fact.pinned ? 'Always used with your data · ' : ''}
          {fact.source_chat ? 'learned in a chat' : 'added by you'} · {relativeTime(fact.updated_at)}
          {' · '}
          <button
            type="button"
            disabled={busy}
            className="press text-ink-300 underline decoration-ink-600 underline-offset-2"
            onClick={() => void run(async () => onSaved(await memoryUpdate(client, fact.id, { kind: fact.kind === 'preference' ? 'fact' : 'preference' })))}
          >
            {fact.kind === 'preference' ? 'Make it a fact' : 'Make it a preference'}
          </button>
        </p>
      </div>
      {editing ? (
        <>
          <IconButton icon="x" label="Cancel" onClick={() => { setText(fact.fact); setEditing(false); }} />
          <IconButton icon="check" label="Save fact" onClick={() => void save()} disabled={busy} className="text-signal" />
        </>
      ) : (
        armed ? (
          <Button variant="danger" icon="trash" onClick={remove} disabled={busy}>
            Delete?
          </Button>
        ) : (
          <IconButton icon="trash" label="Delete fact" disabled={busy} onClick={remove} />
        )
      )}
    </div>
  );
}

/** What the assistant remembers about the owner: view, edit, pin, delete, or forget everything. */
export default function MemoryView() {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const facts = useHubData((c, s) => memoryList(c, undefined, s), []);
  const [draft, setDraft] = useState('');
  const [adding, setAdding] = useState(false);
  usePullRefresh(facts.reload);

  const list = facts.data ?? [];
  const replace = (f: MemoryFact) => facts.setData((xs) => (xs ?? []).map((x) => (x.id === f.id ? f : x)).sort((a, b) => Number(b.pinned) - Number(a.pinned)));

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const t = draft.trim();
    if (!t || adding) return;
    setAdding(true);
    try {
      const f = await memoryCreate(client, t);
      facts.setData((xs) => [f, ...(xs ?? []).filter((x) => x.id !== f.id)]);
      setDraft('');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setAdding(false);
    }
  };

  const forgetAll = async () => {
    const ok = await confirm({
      title: 'Forget everything?',
      body: `The assistant will lose all ${list.length} saved fact${list.length === 1 ? '' : 's'}. This cannot be undone.`,
      confirmLabel: 'Forget everything',
      tone: 'danger',
    });
    if (!ok) return;
    try {
      const r = await memoryForgetAll(client);
      facts.setData(() => []);
      notify(`Forgot ${r.deleted} fact${r.deleted === 1 ? '' : 's'}`, 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  return (
    <div className="space-y-4">
      <p className="px-1 text-[13px] text-ink-400">
        Facts the assistant learned when you told it about yourself (“mon salaire tombe le 25”). Answer preferences (language, tone, length) go with
        every chat, except in Never mode. Other facts are only sent when your question is about your own data: pinned ones always, the rest when
        relevant.
      </p>
      <form onSubmit={(e) => void add(e)} className="flex gap-2">
        <input className={inputClass} value={draft} maxLength={300} onChange={(e) => setDraft(e.target.value)} placeholder="Add a fact, e.g. I prefer answers in Darija" aria-label="New fact" />
        <Button variant="primary" icon="plus" type="submit" disabled={!draft.trim() || adding}>
          Add
        </Button>
      </form>
      {facts.error && <ErrorNote message={facts.error} onRetry={facts.reload} />}
      {facts.loading && !facts.data && <SkeletonList rows={3} />}
      {facts.data && list.length === 0 && <EmptyState icon="book" title="Nothing remembered yet" hint="Tell the assistant a preference or a fact about you and it will keep it here." />}
      {list.length > 0 && (
        <>
          <div className="flex items-center justify-between px-1">
            <Badge>
              {list.length} fact{list.length === 1 ? '' : 's'} · {list.filter((f) => f.pinned).length} pinned
            </Badge>
          </div>
          <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {list.map((f, i) => (
              <div key={f.id} className="rise" style={riseStyle(i)}>
                <FactRow fact={f} onSaved={replace} onDeleted={(id) => facts.setData((xs) => (xs ?? []).filter((x) => x.id !== id))} />
              </div>
            ))}
          </div>
          <Button variant="danger" icon="trash" className="w-full" onClick={() => void forgetAll()}>
            Forget everything
          </Button>
        </>
      )}
    </div>
  );
}
