import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../../components/icons';
import { Markdown } from '../../components/Markdown';
import { Badge, Button, Card, ErrorNote, IconButton, Segmented, Toggle, cx, inputClass } from '../../components/ui';
import { HubError } from '../../lib/api';
import {
  DATA_MODES,
  MAX_ATTACHMENTS,
  assistantChat,
  assistantConfirm,
  attachmentKey,
  attachmentKind,
  usesAssistant,
  type AssistantReply,
  type AttachmentRef,
  type DataMode,
  type UsedTool,
} from '../../lib/assistant';
import { announce, type Topic } from '../../lib/bus';
import {
  NEW_CHAT_TITLE,
  chatTitleFrom,
  isRetryable,
  loadPending,
  localFromThread,
  newChatId,
  saveChat,
  syncPending,
  trimHistory,
  type LocalChat,
} from '../../lib/chats';
import { scrollToEnd } from '../../lib/dom';
import { formatMs } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { readPref, writePref } from '../../lib/settings';
import type { ChatMessage, ChatSummary, ChatThreadMessage, StoredObject, Usage } from '../../lib/types';
import { ActionCard, AttachSheet, AttachmentChips, ToolChips, type ActionState, type DraftAttachment } from './AssistantBits';
import ChatHistorySheet from './ChatHistorySheet';
import ModelPicker, { parseModelChoice } from './ModelPicker';
import TranscribePanel from './TranscribePanel';
import VoiceAssistant from './VoiceAssistant';

interface Turn {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  routedVia?: string;
  model?: string;
  attempts?: string[];
  latencyMs?: number | null;
  usage?: Usage | null;
  error?: string;
  pending?: boolean;
  /** Assistant turns (/assistant/chat): data route, tools used, writes to confirm, files sent. */
  routed?: 'data' | 'general';
  tools?: UsedTool[];
  actions?: ActionState[];
  attachments?: AttachmentRef[];
}

/** What a confirmed write changes, so open lists reload. */
const TOOL_TOPICS: Record<string, Topic[]> = {
  tasks_add: ['tasks'],
  expense_add: ['expenses', 'money'],
  income_add: ['money'],
  notes_create: ['notes'],
};

interface Meta {
  title: string;
  titleSource: ChatSummary['title_source'];
  createdAt: string;
  pinned: boolean;
}

/** idle: nothing to save yet; pending: kept on this device, retried until the hub accepts it. */
type SyncState = 'idle' | 'loading' | 'saving' | 'saved' | 'pending';

const freshMeta = (): Meta => ({ title: NEW_CHAT_TITLE, titleSource: 'auto', createdAt: new Date().toISOString(), pinned: false });

const SYNC_LABEL: Record<SyncState, string> = {
  idle: 'Saved after the first reply',
  loading: 'Opening...',
  saving: 'Saving...',
  saved: 'Saved, synced across devices',
  pending: 'Offline: kept on this device, will sync',
};

export default function ChatPanel() {
  const { client, notify } = useHub();
  const models = useHubData((c, s) => c.models(s));
  const [choice, setChoice] = useState<string>(() => readPref('chat.model', ''));
  const [streaming, setStreaming] = useState<boolean>(() => readPref('chat.stream', true));
  const [allowFallback, setAllowFallback] = useState<boolean>(() => readPref('chat.fallback', true));
  const [system, setSystem] = useState<string>(() => readPref('chat.system', ''));
  const [showOptions, setShowOptions] = useState(false);
  const [showTranscribe, setShowTranscribe] = useState(false);
  const [dataMode, setDataMode] = useState<DataMode>(() => readPref<DataMode>('assistant.mode', 'auto'));
  const [autoConfirm, setAutoConfirm] = useState<boolean>(() => readPref('assistant.autoConfirm', false));
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  const [drafts, setDrafts] = useState<DraftAttachment[]>([]);
  const draftId = useRef(1);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState<string>(() => {
    const draft = readPref('chat.draft', '');
    if (draft) writePref('chat.draft', '');
    return draft;
  });
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const idRef = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // The conversation being shown: reopened from `chat.current` on mount, saved after every exchange.
  const resumeId = useRef<string>(readPref('chat.current', ''));
  const [chatId, setChatId] = useState<string>(() => newChatId());
  const [meta, setMeta] = useState<Meta>(freshMeta);
  const [sync, setSync] = useState<SyncState>(resumeId.current ? 'loading' : 'idle');
  const [openError, setOpenError] = useState<{ id: string; message: string } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyVersion, setHistoryVersion] = useState(0);
  const dirtyRef = useRef(false);
  const titledRef = useRef(new Set<string>());
  const turnsRef = useRef(turns);
  turnsRef.current = turns;
  const chatIdRef = useRef(chatId);
  chatIdRef.current = chatId;
  const metaRef = useRef(meta);
  metaRef.current = meta;

  // Effect bodies are blocks on purpose: whatever an effect returns becomes its cleanup, and
  // Element.scrollIntoView() returns a Promise in recent Chromium, which React then calls as a
  // function on the next commit ("TypeError: n is not a function", whole-app black screen).
  useEffect(() => {
    writePref('chat.model', choice);
  }, [choice]);
  useEffect(() => {
    writePref('chat.stream', streaming);
  }, [streaming]);
  useEffect(() => {
    writePref('chat.fallback', allowFallback);
  }, [allowFallback]);
  useEffect(() => {
    writePref('chat.system', system);
  }, [system]);
  useEffect(() => {
    writePref('assistant.mode', dataMode);
  }, [dataMode]);
  useEffect(() => {
    writePref('assistant.autoConfirm', autoConfirm);
  }, [autoConfirm]);
  useEffect(() => {
    scrollToEnd(endRef.current);
  }, [turns]);
  // Remember the conversation to reopen, except while one is still opening or failed to open.
  useEffect(() => {
    if (sync !== 'loading' && !openError) writePref('chat.current', chatId);
  }, [chatId, sync, openError]);
  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const patch = (id: number, fn: (t: Turn) => Turn) => setTurns((ts) => ts.map((t) => (t.id === id ? fn(t) : t)));

  const showChat = (c: LocalChat, synced: boolean) => {
    setChatId(c.id);
    setMeta({ title: c.title, titleSource: c.title_source, createdAt: c.created_at, pinned: c.pinned });
    setTurns(
      c.messages
        .filter((m) => m.role !== 'system')
        .map((m) => ({
          id: idRef.current++,
          role: m.role as Turn['role'],
          content: m.content,
          routedVia: m.routed_via,
          model: m.model,
          ...(m.attachments?.length ? { attachments: m.attachments } : {}),
          ...(m.tools?.length ? { tools: m.tools } : {}),
        })),
    );
    setSync(synced ? 'saved' : 'pending');
    setOpenError(null);
  };

  const startNew = () => {
    setChatId(newChatId());
    setMeta(freshMeta());
    setTurns([]);
    setSync('idle');
    setOpenError(null);
    setHistoryOpen(false);
    setDrafts([]);
  };

  /** Reopens a saved conversation; the queued local copy wins over the hub's (it is newer). */
  const openChat = async (id: string) => {
    setHistoryOpen(false);
    const local = loadPending()[id];
    if (local) {
      showChat(local, false);
      return;
    }
    setSync('loading');
    setOpenError(null);
    try {
      showChat(localFromThread(await client.chatGet(id)), true);
    } catch (err) {
      if (err instanceof HubError && err.status === 404) {
        startNew();
        return;
      }
      // Never continue under that id without its messages: the next save would overwrite them.
      setChatId(newChatId());
      setMeta(freshMeta());
      setTurns([]);
      setSync('idle');
      setOpenError({ id, message: errorText(err) });
    }
  };

  useEffect(() => {
    if (resumeId.current) void openChat(resumeId.current);
    // Only on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Saves the current conversation (PUT, idempotent); kept locally and retried when the hub is unreachable. */
  const persist = async () => {
    const id = chatIdRef.current;
    const m = metaRef.current;
    const current = turnsRef.current.filter((t) => t.content.trim() && !t.pending);
    if (!current.length) return;
    const messages: ChatThreadMessage[] = current.map((t) => ({
      role: t.role,
      content: t.content,
      ...(t.routedVia ? { routed_via: t.routedVia } : {}),
      ...(t.model ? { model: t.model } : {}),
      ...(t.attachments?.length ? { attachments: t.attachments.map(({ provider, key, name, content_type, size }) => ({ provider, key, name, content_type, size })) } : {}),
      ...(t.tools?.length ? { tools: t.tools.map(({ name, summary, ok }) => ({ name, summary, ok })) } : {}),
    }));
    const lastModel = [...current].reverse().find((t) => t.role === 'assistant' && t.model)?.model ?? null;
    const title = m.titleSource === 'auto' ? chatTitleFrom(messages) : m.title;
    const local: LocalChat = {
      id,
      title,
      title_source: m.titleSource,
      model: lastModel,
      messages,
      pinned: m.pinned,
      created_at: m.createdAt,
      updated_at: new Date().toISOString(),
    };
    setMeta((x) => ({ ...x, title }));
    setSync('saving');
    try {
      const saved = await saveChat(client, local);
      if (chatIdRef.current === id) {
        setMeta((x) => ({ ...x, title: saved.title, titleSource: saved.title_source, createdAt: saved.created_at, pinned: saved.pinned }));
        setSync('saved');
      }
      setHistoryVersion((v) => v + 1);
      if (saved.title_source === 'auto' && saved.message_count >= 2 && !titledRef.current.has(id)) {
        titledRef.current.add(id);
        client
          .chatAutoTitle(id)
          .then((t) => {
            if (chatIdRef.current === id) setMeta((x) => ({ ...x, title: t.title, titleSource: t.title_source }));
            setHistoryVersion((v) => v + 1);
          })
          .catch(() => undefined);
      }
    } catch (err) {
      const retry = isRetryable(err);
      if (chatIdRef.current === id) setSync(retry ? 'pending' : 'idle');
      if (!retry) notify(`Conversation not saved: ${errorText(err)}`, 'error');
    }
  };

  useEffect(() => {
    if (!busy && dirtyRef.current) {
      dirtyRef.current = false;
      void persist();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  // Retry queued conversations: now, when the device comes back online, and every 30 s.
  useEffect(() => {
    let running = false;
    const retry = async () => {
      if (running || !Object.keys(loadPending()).length) return;
      running = true;
      try {
        const before = new Set(Object.keys(loadPending()));
        await syncPending(client);
        const id = chatIdRef.current;
        if (before.has(id) && !(id in loadPending())) setSync((s) => (s === 'pending' ? 'saved' : s));
        setHistoryVersion((v) => v + 1);
      } finally {
        running = false;
      }
    };
    void retry();
    const onOnline = () => {
      void retry();
    };
    window.addEventListener('online', onOnline);
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void retry();
    }, 30_000);
    return () => {
      window.removeEventListener('online', onOnline);
      window.clearInterval(timer);
    };
  }, [client]);

  // ---------------------------------------------------------------- attachments (uploaded to Drive chat/YYYY/MM/ first)

  const patchDraft = (id: number, p: Partial<DraftAttachment>) => setDrafts((ds) => ds.map((d) => (d.id === id ? { ...d, ...p } : d)));

  const attachFiles = (files: File[]) => {
    setAttachOpen(false);
    const room = MAX_ATTACHMENTS - drafts.length;
    if (files.length > room) notify(`At most ${MAX_ATTACHMENTS} files per message`, 'error');
    for (const file of files.slice(0, Math.max(0, room))) {
      const check = attachmentKind(file.name, file.type, file.size);
      if ('error' in check) {
        notify(check.error, 'error');
        continue;
      }
      const id = draftId.current++;
      setDrafts((ds) => [...ds, { id, name: file.name, size: file.size, progress: 0 }]);
      // Large files go straight to the storage backend (presigned PUT), never through the hub's body limit.
      client
        .driveUploadFile(file, { key: attachmentKey(file.name), onProgress: (p) => patchDraft(id, { progress: p }) })
        .then((res) => patchDraft(id, { progress: 1, ref: { provider: res.provider, key: res.key, name: file.name, content_type: file.type || null, size: file.size } }))
        .catch((err) => patchDraft(id, { error: errorText(err) }));
    }
  };

  const attachDrive = (obj: StoredObject) => {
    setAttachOpen(false);
    const name = obj.key.split('/').pop() || obj.key;
    const check = attachmentKind(name, obj.content_type, obj.size);
    if ('error' in check) {
      notify(check.error, 'error');
      return;
    }
    if (drafts.length >= MAX_ATTACHMENTS) {
      notify(`At most ${MAX_ATTACHMENTS} files per message`, 'error');
      return;
    }
    const id = draftId.current++;
    setDrafts((ds) => [...ds, { id, name, size: obj.size, progress: 1, ref: { provider: obj.backend, key: obj.key, name, content_type: obj.content_type ?? null, size: obj.size } }]);
  };

  const uploading = drafts.some((d) => !d.ref && !d.error);

  // ---------------------------------------------------------------- confirmations of assistant writes

  const setAction = (turnId: number, actionId: string, p: Partial<ActionState>) =>
    patch(turnId, (t) => ({ ...t, actions: t.actions?.map((a) => (a.id === actionId ? { ...a, ...p } : a)) }));

  const confirmAction = async (turnId: number, action: ActionState): Promise<boolean> => {
    setAction(turnId, action.id, { status: 'running', error: undefined });
    try {
      const res = await assistantConfirm(client, action);
      setAction(turnId, action.id, { status: 'done' });
      announce(...(TOOL_TOPICS[action.tool] ?? []));
      notify(`Created: ${res.summary}`, 'ok');
      return true;
    } catch (err) {
      setAction(turnId, action.id, { status: 'error', error: errorText(err) });
      return false;
    }
  };

  const waitingActions = () =>
    turnsRef.current.flatMap((t) => (t.actions ?? []).filter((a) => a.status === 'waiting' || a.status === 'error').map((a) => ({ turnId: t.id, action: a })));

  /** Voice "oui": confirms everything still waiting; returns a short sentence to speak. */
  const confirmAllWaiting = async (): Promise<string> => {
    const items = waitingActions();
    if (!items.length) return 'Rien à confirmer.';
    let ok = 0;
    for (const { turnId, action } of items) if (await confirmAction(turnId, action)) ok++;
    return ok === items.length ? `C'est fait : ${items.map((i) => i.action.summary).join(', ')}.` : `${ok} sur ${items.length} créés, voir les erreurs.`;
  };

  const dismissAllWaiting = () => {
    for (const { turnId, action } of waitingActions()) setAction(turnId, action.id, { status: 'dismissed' });
  };

  // ---------------------------------------------------------------- send

  /** Sends `override` (voice) or the input box. Returns the assistant reply when it went through /assistant/chat. */
  const send = async (override?: string): Promise<AssistantReply | null> => {
    const text = (override ?? input).trim();
    const files = override === undefined ? drafts.filter((d) => d.ref).map((d) => d.ref as AttachmentRef) : [];
    if ((!text && !files.length) || busy || (override === undefined && uploading)) return null;
    // Voice turns always go through the assistant (it answers in JSON to speak; "Never" still keeps data out).
    const viaAssistant = override !== undefined || usesAssistant(dataMode, files.length);
    const userTurn: Turn = { id: idRef.current++, role: 'user', content: text || 'What is in this file?', ...(files.length ? { attachments: files } : {}) };
    const reply: Turn = { id: idRef.current++, role: 'assistant', content: '', pending: true };
    // The whole conversation goes back to the model; the oldest turns are dropped when it gets too long.
    const history: ChatMessage[] = trimHistory([
      ...(system.trim() ? [{ role: 'system' as const, content: system.trim() }] : []),
      ...turns.filter((t) => !t.error && t.content).map((t) => ({ role: t.role, content: t.content })),
      { role: 'user', content: userTurn.content },
    ]);
    setTurns((ts) => [...ts, userTurn, reply]);
    if (override === undefined) {
      setInput('');
      setDrafts([]);
    }
    setOpenError(null);
    dirtyRef.current = true;
    setBusy(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const { provider, model } = parseModelChoice(choice);
    const req = { messages: history, model, provider, allow_fallback: allowFallback };
    const started = performance.now();
    try {
      if (viaAssistant) {
        const res = await assistantChat(
          client,
          { ...req, mode: dataMode, auto_confirm: autoConfirm, chat_id: chatIdRef.current, attachments: files },
          ctrl.signal,
        );
        patch(reply.id, (t) => ({
          ...t,
          pending: false,
          content: res.content,
          routedVia: res.routed_via || undefined,
          model: res.model || undefined,
          latencyMs: res.latency_ms ?? performance.now() - started,
          routed: res.routed,
          tools: res.used_tools,
          actions: res.pending_actions.map((a) => ({ ...a, status: 'waiting' as const })),
        }));
        if (res.attachments.length) patch(userTurn.id, (t) => ({ ...t, attachments: res.attachments }));
        if (autoConfirm) {
          const topics = res.used_tools.flatMap((u) => TOOL_TOPICS[u.name] ?? []);
          if (topics.length) announce(...topics);
        }
        if (res.remembered.length) notify(`Remembered: ${res.remembered.map((m) => m.fact).join(' · ')}`, 'ok');
        return res;
      }
      if (streaming) {
        await client.stream(
          req,
          {
            onRoutedVia: (rv) => patch(reply.id, (t) => ({ ...t, routedVia: rv })),
            onChunk: (chunk) =>
              patch(reply.id, (t) => ({
                ...t,
                content: t.content + (chunk.delta ?? ''),
                routedVia: chunk.routed_via || t.routedVia,
                model: chunk.model || t.model,
                usage: chunk.usage ?? t.usage,
              })),
          },
          ctrl.signal,
        );
        patch(reply.id, (t) => ({ ...t, pending: false, latencyMs: performance.now() - started }));
      } else {
        const { response, routedVia } = await client.chat(req, ctrl.signal);
        patch(reply.id, (t) => ({
          ...t,
          pending: false,
          content: response.content ?? '',
          routedVia,
          model: response.model,
          attempts: response.attempts,
          latencyMs: response.latency_ms ?? performance.now() - started,
          usage: response.usage,
        }));
      }
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === 'AbortError';
      patch(reply.id, (t) => ({ ...t, pending: false, error: aborted ? 'Stopped.' : errorText(err) }));
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
    return null;
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const turnCount = turns.filter((t) => t.content.trim()).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => setHistoryOpen(true)}
          disabled={busy}
          className="press min-w-0 flex-1 rounded-row px-1 py-1 text-left disabled:opacity-60"
          aria-label={`Conversation: ${meta.title}. Open history`}
        >
          <span className="block truncate font-display text-[16px] font-semibold text-ink-100">{meta.title}</span>
          <span className={cx('flex items-center gap-1.5 text-[12px]', sync === 'pending' ? 'text-amber-400' : 'text-ink-500')}>
            {sync === 'pending' && <Icon name="clock" size={12} />}
            {sync === 'saved' && <Icon name="check" size={12} className="text-mint-400" />}
            <span className="truncate">{SYNC_LABEL[sync]}</span>
            {turnCount > 0 && <span className="shrink-0 text-ink-600">· {turnCount} msg</span>}
          </span>
        </button>
        <IconButton icon="clock" label="Chat history" onClick={() => setHistoryOpen(true)} disabled={busy} />
        <IconButton icon="plus" label="New chat" onClick={startNew} disabled={busy || (turns.length === 0 && !openError)} />
      </div>
      {openError && (
        <ErrorNote
          message={`Could not open the last conversation (${openError.message}). Messages you send now start a new chat.`}
          onRetry={() => void openChat(openError.id)}
        />
      )}

      <Card className="space-y-3 p-3">
        <div className="flex gap-2">
          <div className="min-w-0 flex-1">
            <ModelPicker models={models.data} value={choice} onChange={setChoice} disabled={busy} />
          </div>
          <IconButton
            icon="wave"
            label="Transcribe audio"
            onClick={() => setShowTranscribe((v) => !v)}
            className={showTranscribe ? 'text-signal' : ''}
          />
          <IconButton icon="settings" label="Chat options" onClick={() => setShowOptions((v) => !v)} className={showOptions ? 'text-signal' : ''} />
        </div>
        {models.error && <p className="text-xs text-danger-400">Model list unavailable: {models.error}</p>}
        <div className="flex items-center gap-3">
          <span className="shrink-0 text-[13px] text-ink-300">Use my data</span>
          <div className="min-w-0 flex-1">
            <Segmented value={dataMode} onChange={setDataMode} options={DATA_MODES} />
          </div>
        </div>
        {showOptions && (
          <div className="space-y-3 border-t border-ink-800 pt-3">
            <label className="flex items-center justify-between gap-3 text-sm text-ink-200">
              <span>
                Create without asking
                <span className="block text-[12px] text-ink-500">Tasks, expenses and notes the assistant proposes are created at once</span>
              </span>
              <Toggle checked={autoConfirm} onChange={setAutoConfirm} label="Auto-confirm" />
            </label>
            <label className="flex items-center justify-between gap-3 text-sm text-ink-200">
              Stream tokens (SSE, when not using your data)
              <Toggle checked={streaming} onChange={setStreaming} label="Stream tokens" />
            </label>
            <label className="flex items-center justify-between gap-3 text-sm text-ink-200">
              Fail over to other providers
              <Toggle checked={allowFallback} onChange={setAllowFallback} label="Allow fallback" />
            </label>
            <label className="block space-y-1 text-sm text-ink-200">
              System prompt
              <textarea className={cx(inputClass, 'min-h-[64px] py-2')} value={system} onChange={(e) => setSystem(e.target.value)} placeholder="Optional" />
            </label>
            {turns.length > 0 && (
              <Button variant="ghost" icon="plus" onClick={startNew} disabled={busy}>
                Start a new chat
              </Button>
            )}
          </div>
        )}
      </Card>

      {showTranscribe && (
        <TranscribePanel
          onClose={() => setShowTranscribe(false)}
          onSend={(text) => {
            setInput((cur) => (cur.trim() ? `${cur.trimEnd()}
${text}` : text));
            setShowTranscribe(false);
            inputRef.current?.focus();
          }}
        />
      )}

      <div className="space-y-3" aria-live="polite">
        {turns.length === 0 && sync !== 'loading' && (
          <p className="px-2 py-8 text-center text-sm text-ink-400">
            Ask anything, or about your own money, tasks, agenda and notes ({dataMode === 'never' ? 'off: “Use my data” is Never' : dataMode === 'always' ? 'always on' : 'used only when the question needs it'}). Attach photos
            or PDFs, or talk with the mic. Past conversations are in the history.
          </p>
        )}
        {turns.map((t) =>
          t.role === 'user' ? (
            <div key={t.id} className="rise ml-10 space-y-2 whitespace-pre-wrap rounded-[20px] rounded-br-md bg-signal/15 px-4 py-2.5 text-[15px] text-ink-100">
              {t.attachments && t.attachments.length > 0 && <AttachmentChips items={t.attachments} />}
              <div>{t.content}</div>
            </div>
          ) : (
            <div key={t.id} className="rise mr-4 space-y-2">
              <div className="flex flex-wrap items-center gap-1.5">
                {t.routedVia ? (
                  <Badge tone="signal" className="gap-1">
                    <Icon name="bolt" size={11} />
                    {t.routedVia}
                  </Badge>
                ) : (
                  t.pending && <Badge>routing...</Badge>
                )}
                {t.model && <Badge>{t.model}</Badge>}
                {t.latencyMs != null && <Badge>{formatMs(t.latencyMs)}</Badge>}
                {t.usage && t.usage.total_tokens > 0 && <Badge>{t.usage.total_tokens} tok</Badge>}
              </div>
              <ToolChips tools={t.tools} routed={t.routed} />
              {t.attempts && t.attempts.length > 0 && (
                <p className="text-[12px] text-amber-400">failover: {t.attempts.join(' -> ')}</p>
              )}
              {(t.content || t.pending) && (
                <div className="min-w-0 break-words rounded-[20px] rounded-bl-md bg-ink-900 px-4 py-2.5 text-[15px] leading-relaxed ring-1 ring-inset ring-ink-800 text-ink-100">
                  {t.content && <Markdown text={t.content} className="text-ink-100" />}
                  {t.pending && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-signal align-middle" />}
                </div>
              )}
              {t.actions?.map((a) => (
                <ActionCard
                  key={a.id}
                  action={a}
                  onConfirm={() => void confirmAction(t.id, a)}
                  onDismiss={() => setAction(t.id, a.id, { status: 'dismissed' })}
                />
              ))}
              {t.error && <ErrorNote message={t.error} />}
            </div>
          ),
        )}
        <div ref={endRef} />
      </div>

      <ChatHistorySheet
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        currentId={chatId}
        version={historyVersion}
        onOpen={(id) => void openChat(id)}
        onNew={startNew}
        onChanged={(c) => {
          if (c.id === chatIdRef.current) setMeta((x) => ({ ...x, title: c.title, titleSource: c.title_source, pinned: c.pinned }));
        }}
        onDeleted={(id) => {
          if (id === chatIdRef.current) startNew();
        }}
      />

      <AttachSheet open={attachOpen} onClose={() => setAttachOpen(false)} onPickFiles={attachFiles} onPickDrive={attachDrive} />

      <div className="sticky -mx-4 space-y-2 bg-gradient-to-t from-ink-950 via-ink-950/95 to-transparent px-4 pb-2 pt-4" style={{ bottom: 'calc(var(--nav-h) + var(--safe-bottom) + 8px)' }}>
        {voiceOpen && (
          <VoiceAssistant
            ask={(text) => send(text)}
            hasPending={turns.some((t) => t.actions?.some((a) => a.status === 'waiting'))}
            onConfirmPending={confirmAllWaiting}
            onDismissPending={dismissAllWaiting}
            onClose={() => setVoiceOpen(false)}
          />
        )}
        <AttachmentChips items={drafts} onRemove={(i) => setDrafts((ds) => ds.filter((_, j) => j !== i))} />
        <div className="flex items-end gap-1 rounded-[24px] bg-ink-900 p-1.5 ring-1 ring-inset ring-ink-700 focus-within:ring-signal/60">
          <IconButton icon="plus" label="Attach a photo, PDF or Drive file" onClick={() => setAttachOpen(true)} disabled={busy || drafts.length >= MAX_ATTACHMENTS} />
          <textarea
            ref={inputRef}
            aria-label="Message"
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            placeholder={dataMode === 'never' ? 'Message the pool' : 'Ask anything'}
            className="max-h-40 min-h-[44px] flex-1 resize-none bg-transparent px-1 py-2.5 text-[16px] text-ink-100 placeholder:text-ink-500 focus:outline-none"
          />
          {busy ? (
            <IconButton icon="stop" label="Stop" onClick={() => abortRef.current?.abort()} className="bg-ink-800 text-danger-400" />
          ) : (
            <>
              {!input.trim() && !drafts.length && (
                <IconButton icon="mic" label="Voice assistant" onClick={() => setVoiceOpen((v) => !v)} className={voiceOpen ? 'text-signal' : ''} />
              )}
              <IconButton
                icon="send"
                label="Send"
                onClick={() => void send()}
                disabled={(!input.trim() && !drafts.some((d) => d.ref)) || uploading || sync === 'loading'}
                className="bg-signal text-ink-950 hover:bg-signal/90 hover:text-ink-950"
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
