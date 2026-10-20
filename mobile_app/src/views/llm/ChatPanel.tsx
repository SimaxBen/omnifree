import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../../components/icons';
import { Badge, Button, Card, ErrorNote, IconButton, Toggle, cx, inputClass } from '../../components/ui';
import { scrollToEnd } from '../../lib/dom';
import { formatMs } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { readPref, writePref } from '../../lib/settings';
import type { ChatMessage, Usage } from '../../lib/types';
import ModelPicker, { parseModelChoice } from './ModelPicker';
import TranscribePanel from './TranscribePanel';

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
}

export default function ChatPanel() {
  const { client } = useHub();
  const models = useHubData((c, s) => c.models(s));
  const [choice, setChoice] = useState<string>(() => readPref('chat.model', ''));
  const [streaming, setStreaming] = useState<boolean>(() => readPref('chat.stream', true));
  const [allowFallback, setAllowFallback] = useState<boolean>(() => readPref('chat.fallback', true));
  const [system, setSystem] = useState<string>(() => readPref('chat.system', ''));
  const [showOptions, setShowOptions] = useState(false);
  const [showTranscribe, setShowTranscribe] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const idRef = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

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
    scrollToEnd(endRef.current);
  }, [turns]);
  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const patch = (id: number, fn: (t: Turn) => Turn) => setTurns((ts) => ts.map((t) => (t.id === id ? fn(t) : t)));

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    const userTurn: Turn = { id: idRef.current++, role: 'user', content: text };
    const reply: Turn = { id: idRef.current++, role: 'assistant', content: '', pending: true };
    const history: ChatMessage[] = [
      ...(system.trim() ? [{ role: 'system' as const, content: system.trim() }] : []),
      ...turns.filter((t) => !t.error && t.content).map((t) => ({ role: t.role, content: t.content })),
      { role: 'user', content: text },
    ];
    setTurns((ts) => [...ts, userTurn, reply]);
    setInput('');
    setBusy(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const { provider, model } = parseModelChoice(choice);
    const req = { messages: history, model, provider, allow_fallback: allowFallback };
    const started = performance.now();
    try {
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
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="space-y-3">
      <Card className="space-y-3 p-3">
        <div className="flex gap-2">
          <div className="min-w-0 flex-1">
            <ModelPicker models={models.data} value={choice} onChange={setChoice} disabled={busy} />
          </div>
          <IconButton
            icon="mic"
            label="Transcribe audio"
            onClick={() => setShowTranscribe((v) => !v)}
            className={showTranscribe ? 'text-signal' : ''}
          />
          <IconButton icon="settings" label="Chat options" onClick={() => setShowOptions((v) => !v)} className={showOptions ? 'text-signal' : ''} />
        </div>
        {models.error && <p className="text-xs text-danger-400">Model list unavailable: {models.error}</p>}
        {showOptions && (
          <div className="space-y-3 border-t border-ink-800 pt-3">
            <label className="flex items-center justify-between gap-3 text-sm text-ink-200">
              Stream tokens (SSE)
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
              <Button variant="ghost" icon="trash" onClick={() => setTurns([])} disabled={busy}>
                Clear conversation
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
        {turns.length === 0 && (
          <p className="px-2 py-8 text-center text-sm text-ink-400">
            Ask anything. Each reply shows which provider answered (<span className="font-mono text-ink-300">X-Routed-Via</span>) and any failovers on the way.
          </p>
        )}
        {turns.map((t) =>
          t.role === 'user' ? (
            <div key={t.id} className="ml-10 whitespace-pre-wrap rounded-xl rounded-br-sm bg-ink-800 px-3 py-2 text-sm text-ink-100">
              {t.content}
            </div>
          ) : (
            <div key={t.id} className="mr-4 space-y-2">
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
              {t.attempts && t.attempts.length > 0 && (
                <p className="font-mono text-[11px] text-amber-400">failover: {t.attempts.join(' -> ')}</p>
              )}
              {(t.content || t.pending) && (
                <div className="whitespace-pre-wrap break-words rounded-xl rounded-bl-sm border border-ink-700/70 bg-ink-900 px-3 py-2 text-sm leading-relaxed text-ink-100">
                  {t.content}
                  {t.pending && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-signal align-middle" />}
                </div>
              )}
              {t.error && <ErrorNote message={t.error} />}
            </div>
          ),
        )}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-0 -mx-4 bg-gradient-to-t from-ink-950 via-ink-950 to-transparent px-4 pb-1 pt-4">
        <div className="flex items-end gap-2 rounded-xl border border-ink-700 bg-ink-900 p-2">
          <textarea
            ref={inputRef}
            aria-label="Message"
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            placeholder="Message the pool"
            className="max-h-40 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none"
          />
          {busy ? (
            <IconButton icon="stop" label="Stop" onClick={() => abortRef.current?.abort()} className="bg-ink-800 text-danger-400" />
          ) : (
            <IconButton icon="send" label="Send" onClick={() => void send()} disabled={!input.trim()} className="bg-signal text-ink-950 hover:bg-signal/90 hover:text-ink-950" />
          )}
        </div>
      </div>
    </div>
  );
}
