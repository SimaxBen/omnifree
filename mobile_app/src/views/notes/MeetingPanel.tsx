import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Markdown } from '../../components/Markdown';
import { RecordButton, canRecord, extensionFor, formatClock } from '../../components/Recorder';
import { Button, ErrorNote, Meter, cx, haptic, inputClass, riseStyle } from '../../components/ui';
import { isoDay } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import {
  AUDIO_BITS_PER_SECOND,
  PART_SECONDS,
  finishMeeting,
  meetingPartKey,
  meetingProgress,
  meetingStatusText,
  newSession,
  pickMime,
  postMeeting,
  type MeetingStatus,
} from '../../lib/meeting';
import type { Note, Task } from '../../lib/types';
import { TaskRow } from './TaskRow';

type Phase = 'idle' | 'finishing' | 'done';

interface WakeLockLike {
  release: () => Promise<void>;
}

/**
 * Records in parts of PART_SECONDS: a new MediaRecorder starts on the same stream, then the previous one stops,
 * so there is no gap and every part is a complete file. `onPart` gets each part in order; `onEnd` runs after the
 * last one.
 */
function useSegmentedRecorder(onPart: (blob: Blob, index: number, seconds: number) => void, onEnd: () => void) {
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const current = useRef<MediaRecorder | null>(null);
  const index = useRef(0);
  const started = useRef(0);
  const segStarted = useRef(0);
  const stopping = useRef(false);
  const audioCtx = useRef<AudioContext | null>(null);
  const raf = useRef(0);
  const wake = useRef<WakeLockLike | null>(null);
  const cb = useRef({ onPart, onEnd });
  cb.current = { onPart, onEnd };

  const teardown = useCallback(() => {
    cancelAnimationFrame(raf.current);
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    void audioCtx.current?.close().catch(() => undefined);
    audioCtx.current = null;
    void wake.current?.release().catch(() => undefined);
    wake.current = null;
    setLevel(0);
  }, []);

  const startSegment = useCallback(() => {
    const s = stream.current;
    if (!s) return;
    const mime = pickMime((t) => MediaRecorder.isTypeSupported(t));
    const r = new MediaRecorder(s, { ...(mime ? { mimeType: mime } : {}), audioBitsPerSecond: AUDIO_BITS_PER_SECOND });
    const chunks: Blob[] = [];
    const i = index.current++;
    const t0 = Date.now();
    segStarted.current = t0;
    r.ondataavailable = (ev) => {
      if (ev.data.size > 0) chunks.push(ev.data);
    };
    r.onstop = () => {
      const blob = new Blob(chunks, { type: r.mimeType || chunks[0]?.type || 'audio/webm' });
      if (blob.size > 0) cb.current.onPart(blob, i, (Date.now() - t0) / 1000);
      if (stopping.current && r === current.current) {
        current.current = null;
        teardown();
        setRecording(false);
        cb.current.onEnd();
      }
    };
    r.start();
    current.current = r;
  }, [teardown]);

  useEffect(() => {
    if (!recording) return undefined;
    const t = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - started.current) / 1000));
      if (!stopping.current && Date.now() - segStarted.current >= PART_SECONDS * 1000) {
        const old = current.current;
        startSegment();
        old?.stop();
      }
    }, 500);
    return () => window.clearInterval(t);
  }, [recording, startSegment]);

  useEffect(
    () => () => {
      stopping.current = true;
      if (current.current && current.current.state !== 'inactive') current.current.stop();
      teardown();
    },
    [teardown],
  );

  const start = useCallback(async () => {
    setError(null);
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      stream.current = s;
      stopping.current = false;
      index.current = 0;
      try {
        const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (Ctx) {
          const ctx = new Ctx();
          audioCtx.current = ctx;
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 512;
          ctx.createMediaStreamSource(s).connect(analyser);
          const buf = new Uint8Array(analyser.fftSize);
          const tick = () => {
            analyser.getByteTimeDomainData(buf);
            let sum = 0;
            for (const v of buf) sum += ((v - 128) / 128) ** 2;
            setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 4));
            raf.current = requestAnimationFrame(tick);
          };
          tick();
        }
      } catch {
        // The level meter is decorative.
      }
      try {
        // Keeps the screen on: a long recording should not stop when the phone locks itself.
        const wl = (navigator as unknown as { wakeLock?: { request: (t: 'screen') => Promise<WakeLockLike> } }).wakeLock;
        if (wl) wake.current = await wl.request('screen');
      } catch {
        // Not granted (battery saver, desktop): recording still works.
      }
      started.current = Date.now();
      setElapsed(0);
      startSegment();
      setRecording(true);
      haptic([12, 40, 12]);
    } catch (err) {
      teardown();
      setError(`Microphone unavailable: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [startSegment, teardown]);

  const stop = useCallback(() => {
    haptic(14);
    stopping.current = true;
    current.current?.stop();
  }, []);

  return { recording, elapsed, level, error, start, stop };
}

/** "Meeting" mode of the voice sheet: long recording -> parts in the Drive -> transcript, summary note, tasks. */
export default function MeetingPanel({ onOpenNote, onClose }: { onOpenNote: (n: Note) => void; onClose: () => void }) {
  const { client, notify } = useHub();
  const [phase, setPhase] = useState<Phase>('idle');
  const [title, setTitle] = useState('');
  const [status, setStatus] = useState<MeetingStatus | null>(null);
  const [uploaded, setUploaded] = useState(0);
  const [recorded, setRecorded] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const session = useRef(newSession());
  const meetingId = useRef<string | null>(null);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const abort = useRef(new AbortController());
  const titleRef = useRef(title);
  titleRef.current = title;

  useEffect(() => () => abort.current.abort(), []);

  // Each part: upload to the Drive, then tell the hub (which transcribes it while the meeting goes on).
  const onPart = (blob: Blob, index: number, seconds: number) => {
    setRecorded((n) => n + 1);
    queue.current = queue.current.then(async () => {
      const ext = extensionFor(blob.type);
      const key = meetingPartKey(session.current, index, ext, isoDay());
      const file = new File([blob], key.split('/').pop() as string, { type: blob.type || 'audio/webm' });
      let lastErr: unknown = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const up = await client.driveUploadFile(file, { key });
          setUploaded((n) => n + 1);
          const res = await postMeeting(
            client,
            {
              ...(meetingId.current ? { meeting_id: meetingId.current } : {}),
              audio_keys: [{ key: up.key, provider: up.provider || undefined, seconds: Math.round(seconds) }],
              final: false,
              ...(titleRef.current.trim() ? { title: titleRef.current.trim() } : {}),
            },
            abort.current.signal,
          );
          meetingId.current = res.meeting_id;
          setStatus(res);
          return;
        } catch (err) {
          if (err instanceof DOMException && err.name === 'AbortError') return;
          lastErr = err;
          await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
        }
      }
      setError(`Part ${index + 1} could not be saved: ${errorText(lastErr)}`);
    });
  };

  const onEnd = () => {
    setPhase('finishing');
    void queue.current.then(async () => {
      if (!meetingId.current) {
        setError((e) => e ?? 'Nothing was recorded.');
        setPhase('idle');
        return;
      }
      try {
        const done = await finishMeeting(client, meetingId.current, setStatus, abort.current.signal);
        setTasks(done.tasks ?? []);
        setPhase('done');
        announce('notes', 'tasks');
        notify('Meeting note ready', 'ok');
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setError(errorText(err));
      }
    });
  };

  const rec = useSegmentedRecorder(onPart, onEnd);

  const retry = () => {
    if (!meetingId.current) return;
    setError(null);
    onEnd();
  };

  if (phase === 'done' && status?.note) {
    const note = status.note;
    return (
      <div className="space-y-4 pb-2">
        <button type="button" onClick={() => onOpenNote(note)} className="press rise block w-full rounded-card bg-ink-850 p-4 text-left ring-1 ring-inset ring-ink-700">
          <span className="flex items-center gap-2 text-[13px] text-ink-400">
            <Icon name="note" size={14} />
            Note in {note.notebook}
          </span>
          <span className="mt-1 block font-display text-[19px] font-semibold text-ink-100">{note.title}</span>
          {status.summary ? (
            <Markdown text={status.summary} className="mt-2 text-[14px]" />
          ) : (
            <span className="mt-1 block text-[14px] text-ink-400">{status.error || 'Transcript saved.'}</span>
          )}
        </button>
        {(status.decisions?.length ?? 0) > 0 && (
          <div className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(1)}>
            <h3 className="font-display text-[16px] font-semibold text-ink-100">Decisions</h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-[14px] text-ink-200 marker:text-ink-500">
              {status.decisions!.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          </div>
        )}
        {tasks.length > 0 && (
          <div className="space-y-1">
            <h3 className="px-1 font-display text-[16px] font-semibold text-ink-100">
              {tasks.length} action item{tasks.length === 1 ? '' : 's'} added as tasks
            </h3>
            <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
              {tasks.map((t, i) => (
                <div key={t.id} className="rise" style={riseStyle(i + 2)}>
                  <TaskRow task={t} onChange={(nt) => setTasks((ts) => ts.map((x) => (x.id === nt.id ? nt : x)))} compact />
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="flex gap-2">
          <Button className="flex-1" icon="note" onClick={() => onOpenNote(note)}>
            Open note
          </Button>
          <Button variant="primary" className="flex-1" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    );
  }

  const progress = meetingProgress(status);
  return (
    <div className="flex flex-col items-center gap-5 pb-2">
      {phase === 'idle' && !rec.recording && (
        <div className="w-full space-y-2">
          <input aria-label="Meeting title (optional)" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Meeting title (optional)" className={inputClass} />
          <p className="px-1 text-center text-[13px] text-ink-400">
            Records as long as needed. Every 10 minutes a part is saved to your Drive and transcribed; at the end you get a summary, decisions and action items in “Meetings”.
          </p>
        </div>
      )}

      {phase === 'finishing' ? (
        <div className="w-full space-y-3 py-6 text-center">
          <div className="dot-wave font-display text-[32px] leading-none text-signal">
            <span>.</span>
            <span>.</span>
            <span>.</span>
          </div>
          <p className="text-[15px] text-ink-200">{meetingStatusText(status)}</p>
          <Meter value={progress} tone="ok" className="mx-auto h-1.5 max-w-[260px]" />
          <p className="text-[13px] text-ink-500">You can leave this open; long meetings take a minute or two.</p>
        </div>
      ) : canRecord() ? (
        <div className="relative flex flex-col items-center">
          <RecordButton recording={rec.recording} level={rec.level} onPress={() => (rec.recording ? rec.stop() : void rec.start())} />
          <p className="mt-1 font-display text-[28px] font-semibold tabular-nums text-ink-100" aria-live="polite">
            {rec.recording ? formatClock(rec.elapsed) : 'Tap to start the meeting'}
          </p>
          {rec.recording && (
            <p className={cx('mt-1 flex items-center gap-1.5 text-[13px]', uploaded < recorded ? 'text-amber-400' : 'text-ink-400')}>
              <Icon name="upload" size={14} />
              {recorded === 0 ? `First part saved at ${formatClock(PART_SECONDS)}` : `${uploaded} of ${recorded} part${recorded === 1 ? '' : 's'} saved`}
              {status && status.parts_done > 0 && ` · ${status.parts_done} transcribed`}
            </p>
          )}
        </div>
      ) : (
        <p className="py-10 text-center text-[14px] text-ink-400">This browser cannot record audio.</p>
      )}

      {(rec.error || error) && (
        <div className="w-full space-y-2">
          <ErrorNote message={(rec.error || error) as string} />
          {phase === 'finishing' && meetingId.current && (
            <Button className="w-full" icon="refresh" onClick={retry}>
              Try again
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
