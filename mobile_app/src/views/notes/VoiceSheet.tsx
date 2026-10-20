import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Icon } from '../../components/icons';
import { RecordButton, canRecord, formatClock, useRecorder } from '../../components/Recorder';
import { Sheet } from '../../components/Sheet';
import { Button, Chip, ErrorNote, Segmented, riseStyle } from '../../components/ui';
import { plainText } from '../../lib/markdown';
import { announce } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { readPref, writePref } from '../../lib/settings';
import type { Note, Task, VoiceMode, VoiceNoteResult } from '../../lib/types';
import { TaskRow } from './TaskRow';

const MODES: { value: VoiceMode; label: string }[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'note', label: 'Note' },
  { value: 'tasks', label: 'Tasks' },
  { value: 'both', label: 'Both' },
];

const MODE_HINT: Record<VoiceMode, string> = {
  auto: 'Always a note; tasks too when you say something to do.',
  note: 'A cleaned-up note only.',
  tasks: 'Tasks only (a note is kept if no task is heard).',
  both: 'A note and every task in it.',
};

/** Record, send to POST /notes/voice, show what the hub made of it. */
export default function VoiceSheet({ open, onClose, onOpenNote }: { open: boolean; onClose: () => void; onOpenNote: (n: Note) => void }) {
  const { client } = useHub();
  const rec = useRecorder();
  const [mode, setMode] = useState<VoiceMode>(() => readPref<VoiceMode>('voice.mode', 'auto'));
  const [notebook, setNotebook] = useState<string>(() => readPref('voice.notebook', 'Inbox'));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<VoiceNoteResult | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [showTranscript, setShowTranscript] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const books = useHubData((c, s) => c.notes({ limit: 1 }, s), []);

  useEffect(() => {
    writePref('voice.mode', mode);
  }, [mode]);
  useEffect(() => {
    writePref('voice.notebook', notebook);
  }, [notebook]);
  useEffect(
    () => () => {
      abort.current?.abort();
    },
    [],
  );

  const send = async (blob: Blob, filename: string) => {
    setBusy(true);
    setError(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      const res = await client.voiceNote(blob, { mode, notebook, filename }, ctrl.signal);
      setResult(res);
      setTasks(res.tasks);
      announce('notes', 'tasks');
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        const msg = errorText(err);
        setError(/422/.test(msg) || /speech|transcript/i.test(msg) ? `${msg}. Try again a little closer to the microphone.` : msg);
      }
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  // Send as soon as a recording stops.
  const lastClip = useRef<Blob | null>(null);
  useEffect(() => {
    if (rec.clip && rec.clip.blob !== lastClip.current && !result) {
      lastClip.current = rec.clip.blob;
      void send(rec.clip.blob, rec.clip.name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rec.clip]);

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = '';
    if (f) void send(f, f.name);
  };

  const again = () => {
    setResult(null);
    setTasks([]);
    setError(null);
    rec.reset();
  };

  const notebooks = Array.from(new Set(['Inbox', ...(books.data?.notebooks.map((b) => b.name) ?? [])]));

  return (
    <Sheet open={open} onClose={onClose} title={result ? 'Captured' : 'Voice note'}>
      {!result ? (
        <div className="flex flex-col items-center gap-5 pb-2">
          <div className="w-full space-y-2">
            <Segmented value={mode} onChange={setMode} options={MODES} />
            <p className="px-1 text-center text-[13px] text-ink-400">{MODE_HINT[mode]}</p>
          </div>

          <div className="relative flex flex-col items-center">
            {busy ? (
              <div className="grid h-[168px] place-items-center text-center">
                <div className="space-y-3">
                  <div className="dot-wave font-display text-[32px] leading-none text-signal">
                    <span>.</span>
                    <span>.</span>
                    <span>.</span>
                  </div>
                  <p className="text-[15px] text-ink-200">Transcribing and sorting</p>
                  <p className="text-[13px] text-ink-500">Usually a few seconds</p>
                </div>
              </div>
            ) : canRecord() ? (
              <RecordButton recording={rec.recording} level={rec.level} onPress={() => (rec.recording ? rec.stop() : void rec.start())} />
            ) : (
              <p className="py-10 text-center text-[14px] text-ink-400">This browser cannot record. Pick an audio file instead.</p>
            )}
            {!busy && (
              <p className="mt-1 font-display text-[28px] font-semibold tabular-nums text-ink-100" aria-live="polite">
                {rec.recording ? formatClock(rec.elapsed) : 'Tap to record'}
              </p>
            )}
          </div>

          {!rec.recording && !busy && (
            <div className="no-scrollbar -mx-5 flex w-[calc(100%+40px)] gap-2 overflow-x-auto px-5">
              {notebooks.map((n) => (
                <Chip key={n} active={n === notebook} onClick={() => setNotebook(n)}>
                  <Icon name="book" size={14} />
                  {n}
                </Chip>
              ))}
            </div>
          )}

          {(rec.error || error) && (
            <div className="w-full space-y-2">
              <ErrorNote message={(rec.error || error) as string} />
              {rec.clip && error && (
                <Button className="w-full" icon="refresh" onClick={() => void send(rec.clip!.blob, rec.clip!.name)}>
                  Send the recording again
                </Button>
              )}
            </div>
          )}

          {!rec.recording && !busy && (
            <button type="button" onClick={() => fileRef.current?.click()} className="press min-h-[44px] text-[14px] text-ink-400 underline underline-offset-4">
              Use an audio file instead
            </button>
          )}
          <input ref={fileRef} type="file" accept="audio/*,video/webm,video/mp4" className="hidden" onChange={onFile} aria-label="Audio file" />
        </div>
      ) : (
        <div className="space-y-4 pb-2">
          {rec.clip && <audio src={rec.clip.url} controls className="w-full" />}

          {result.note && (
            <button
              type="button"
              onClick={() => onOpenNote(result.note as Note)}
              className="press rise block w-full rounded-card bg-ink-850 p-4 text-left ring-1 ring-inset ring-ink-700"
            >
              <span className="flex items-center gap-2 text-[13px] text-ink-400">
                <Icon name="note" size={14} />
                Note in {result.note.notebook}
              </span>
              <span className="mt-1 block font-display text-[19px] font-semibold text-ink-100">{result.note.title}</span>
              <span className="mt-1 line-clamp-3 block text-[14px] text-ink-300">{plainText(result.note.body, 220)}</span>
              {result.note.tags.length > 0 && <span className="mt-2 block text-[13px] text-signal">{result.note.tags.map((t) => `#${t}`).join(' ')}</span>}
            </button>
          )}

          {tasks.length > 0 && (
            <div className="space-y-1">
              <h3 className="px-1 font-display text-[16px] font-semibold text-ink-100">
                {tasks.length} task{tasks.length === 1 ? '' : 's'} added
              </h3>
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
                {tasks.map((t, i) => (
                  <div key={t.id} className="rise" style={riseStyle(i + 1)}>
                    <TaskRow task={t} onChange={(nt) => setTasks((ts) => ts.map((x) => (x.id === nt.id ? nt : x)))} compact />
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <button type="button" onClick={() => setShowTranscript((v) => !v)} className="press flex min-h-[44px] items-center gap-1 px-1 text-[14px] text-ink-400">
              <Icon name={showTranscript ? 'up' : 'down'} size={16} />
              {showTranscript ? 'Hide' : 'Show'} what you said
            </button>
            {showTranscript && <p className="screen-in-up whitespace-pre-wrap rounded-row bg-ink-950 p-3 text-[14px] text-ink-300">{result.transcript}</p>}
          </div>

          <div className="flex gap-2">
            <Button className="flex-1" icon="mic" onClick={again}>
              Record another
            </Button>
            <Button variant="primary" className="flex-1" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      )}
    </Sheet>
  );
}
