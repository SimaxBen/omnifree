import { useCallback, useEffect, useRef, useState } from 'react';
import { canRecord, formatClock, useRecorder } from '../../components/Recorder';
import { Card, ErrorNote, IconButton, Toggle, cx, haptic } from '../../components/ui';
import {
  type AskResult,
  SPEECH_TAGS,
  type SpeechLang,
  busyLine,
  detectLanguage,
  failureLine,
  isSpokenNo,
  isSpokenYes,
  pickVoice,
  speakableText,
} from '../../lib/assistant';
import { errorText, useHub } from '../../lib/hub';
import { readPref, writePref } from '../../lib/settings';

type Phase = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking';

const PHASE_LABEL: Record<Phase, string> = {
  idle: 'Tap the mic and speak',
  listening: 'Listening...',
  transcribing: 'Transcribing...',
  thinking: 'Thinking...',
  speaking: 'Speaking...',
};

// Hands-free: stop listening after this much silence once the owner has started talking.
const SILENCE_LEVEL = 0.07;
const VOICE_LEVEL = 0.16;
const SILENCE_MS = 1600;
const MAX_LISTEN_S = 60;

function canSpeak(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window && typeof SpeechSynthesisUtterance !== 'undefined';
}

/** Speaks `text` with the best installed voice for its language; resolves when done (or skipped). */
function speak(text: string, lang: SpeechLang): Promise<void> {
  return new Promise((resolve) => {
    if (!canSpeak() || !text) {
      resolve();
      return;
    }
    const synth = window.speechSynthesis;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = SPEECH_TAGS[lang];
    const voice = pickVoice(synth.getVoices(), lang);
    if (voice) u.voice = voice;
    u.rate = 1.05;
    let settled = false;
    const done = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    u.onend = done;
    u.onerror = done;
    // Some Android voices never fire onend: give up after a generous estimate.
    window.setTimeout(done, 4000 + text.length * 90);
    synth.speak(u);
  });
}

/**
 * Voice assistant: record -> /llm/transcribe -> /assistant/chat (through `ask`) -> spoken answer.
 * Conversation mode listens again after each answer and stops on silence, so it runs hands-free;
 * "oui / confirme / yes" confirms the creations waiting in the chat, "non / annule" dismisses them.
 */
export default function VoiceAssistant({
  ask,
  hasPending,
  onConfirmPending,
  onDismissPending,
  onClose,
}: {
  ask: (text: string) => Promise<AskResult>;
  hasPending: boolean;
  onConfirmPending: () => Promise<string>;
  onDismissPending: () => void;
  onClose: () => void;
}) {
  const { client } = useHub();
  const rec = useRecorder();
  const [phase, setPhase] = useState<Phase>('idle');
  const [heard, setHeard] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [speakOn, setSpeakOn] = useState<boolean>(() => readPref('assistant.speak', true));
  const [conversation, setConversation] = useState<boolean>(() => readPref('assistant.conversation', false));
  const handled = useRef<string | null>(null);
  const alive = useRef(true);
  const voiced = useRef(false);
  const quietSince = useRef<number | null>(null);
  const pendingRef = useRef(hasPending);
  pendingRef.current = hasPending;

  useEffect(() => {
    writePref('assistant.speak', speakOn);
  }, [speakOn]);
  useEffect(() => {
    writePref('assistant.conversation', conversation);
  }, [conversation]);
  useEffect(() => {
    alive.current = true;
    // Voices load asynchronously on Chromium: touching the list early makes them ready in time.
    if (canSpeak()) window.speechSynthesis.getVoices();
    return () => {
      alive.current = false;
      if (canSpeak()) window.speechSynthesis.cancel();
    };
  }, []);

  const listen = useCallback(() => {
    if (canSpeak()) window.speechSynthesis.cancel();
    setError(null);
    voiced.current = false;
    quietSince.current = null;
    setPhase('listening');
    void rec.start();
  }, [rec]);

  // Stop on silence (hands-free) and after MAX_LISTEN_S in any case.
  useEffect(() => {
    if (!rec.recording) return;
    if (rec.elapsed >= MAX_LISTEN_S) {
      rec.stop();
      return;
    }
    if (!conversation) return;
    if (rec.level > VOICE_LEVEL) {
      voiced.current = true;
      quietSince.current = null;
    } else if (voiced.current && rec.level < SILENCE_LEVEL) {
      const now = Date.now();
      if (quietSince.current === null) quietSince.current = now;
      else if (now - quietSince.current > SILENCE_MS) rec.stop();
    }
  }, [rec, rec.level, rec.elapsed, rec.recording, conversation]);

  useEffect(() => {
    if (rec.error) {
      setError(rec.error);
      setPhase('idle');
    }
  }, [rec.error]);

  const say = useCallback(
    async (text: string, langHint?: SpeechLang) => {
      if (!speakOn || !alive.current) return;
      setPhase('speaking');
      const spoken = speakableText(text);
      await speak(spoken, langHint ?? detectLanguage(spoken));
    },
    [speakOn],
  );

  // A finished clip: transcribe, then confirm/dismiss pending writes or ask the assistant.
  useEffect(() => {
    const clip = rec.clip;
    if (!clip || handled.current === clip.url) return;
    handled.current = clip.url;
    void (async () => {
      setPhase('transcribing');
      try {
        if (clip.seconds < 0.4) throw new Error('Too short: hold on a little longer');
        const t = await client.transcribe(clip.blob, { filename: clip.name });
        const text = (t.text ?? '').trim();
        setHeard(text);
        if (!text) throw new Error('No speech detected');
        if (pendingRef.current && isSpokenYes(text) && text.split(/\s+/).length <= 4) {
          setPhase('thinking');
          await say(await onConfirmPending());
        } else if (pendingRef.current && isSpokenNo(text) && text.split(/\s+/).length <= 4) {
          onDismissPending();
          await say(detectLanguage(text) === 'en' ? 'Cancelled.' : 'Annulé.');
        } else {
          setPhase('thinking');
          const res = await ask(text);
          if (!res.ok) {
            // Say it (the owner may not be looking), then stop: hands-free must not listen again after a failure.
            if (!alive.current) return;
            if (!res.aborted) await say(res.busy ? busyLine(detectLanguage(text)) : failureLine(detectLanguage(text)));
            if (!alive.current) return;
            if (!res.aborted) setError(res.error);
            setPhase('idle');
            return;
          }
          if (res.reply.content) await say(res.reply.content, res.reply.language);
        }
        if (!alive.current) return;
        if (conversation) {
          haptic(8);
          listen();
        } else setPhase('idle');
      } catch (err) {
        if (!alive.current) return;
        setError(errorText(err));
        setPhase('idle');
      } finally {
        rec.reset();
      }
    })();
  }, [rec, rec.clip, client, ask, say, conversation, listen, onConfirmPending, onDismissPending]);

  const busy = phase === 'transcribing' || phase === 'thinking';
  const onMic = () => {
    if (rec.recording) rec.stop();
    else if (phase === 'speaking') {
      if (canSpeak()) window.speechSynthesis.cancel();
      listen();
    } else if (!busy) listen();
  };

  if (!canRecord()) {
    return (
      <Card className="p-3">
        <ErrorNote message="This browser cannot record audio, so the voice assistant is unavailable." />
      </Card>
    );
  }

  return (
    <Card className="space-y-3 p-3">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onMic}
          disabled={busy}
          aria-label={rec.recording ? 'Stop listening' : 'Talk to the assistant'}
          aria-pressed={rec.recording}
          className={cx(
            'relative grid h-16 w-16 shrink-0 place-items-center rounded-full transition-[transform,background-color] duration-base active:scale-90 disabled:opacity-50',
            rec.recording ? 'bg-danger-400 text-ink-950' : phase === 'speaking' ? 'bg-signal text-ink-950' : 'bg-ink-800 text-signal ring-2 ring-inset ring-signal/60',
          )}
          style={rec.recording ? { boxShadow: `0 0 0 ${4 + rec.level * 18}px rgba(255,92,122,0.25)` } : undefined}
        >
          <span className={cx('block', rec.recording ? 'h-5 w-5 rounded-md bg-ink-950' : 'h-6 w-6 rounded-full', !rec.recording && (phase === 'speaking' ? 'animate-pulse bg-ink-950' : 'bg-signal'))} />
        </button>
        <div className="min-w-0 flex-1">
          <p className="font-display text-[16px] font-semibold text-ink-100">
            {PHASE_LABEL[phase]} {rec.recording && <span className="font-mono text-[13px] text-ink-400">{formatClock(rec.elapsed)}</span>}
          </p>
          <p className="truncate text-[13px] text-ink-400">{heard ? `“${heard}”` : 'Ask about your money, tasks or agenda, or say “ajoute une tâche...”'}</p>
        </div>
        <IconButton icon="x" label="Close voice assistant" onClick={onClose} />
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2 border-t border-ink-800 pt-2 text-[13px] text-ink-300">
        <label className="flex items-center gap-2">
          <Toggle checked={speakOn} onChange={setSpeakOn} label="Speak answers" disabled={!canSpeak()} />
          Speak answers
        </label>
        <label className="flex items-center gap-2">
          <Toggle checked={conversation} onChange={setConversation} label="Conversation mode" />
          Hands-free
        </label>
      </div>
      {hasPending && <p className="text-[12px] text-amber-400">Say “oui” / “yes” to create what is waiting in the chat, or “non” to dismiss it.</p>}
      {error && <ErrorNote message={error} />}
    </Card>
  );
}
