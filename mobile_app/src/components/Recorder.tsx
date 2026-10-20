import { useCallback, useEffect, useRef, useState } from 'react';
import { cx, haptic } from './ui';

export interface Clip {
  blob: Blob;
  name: string;
  url: string;
  seconds: number;
}

export function extensionFor(mime: string): string {
  if (mime.includes('ogg')) return 'ogg';
  if (mime.includes('mp4') || mime.includes('aac')) return 'm4a';
  if (mime.includes('wav')) return 'wav';
  if (mime.includes('mpeg')) return 'mp3';
  return 'webm';
}

export function canRecord(): boolean {
  return typeof MediaRecorder !== 'undefined' && typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia;
}

/** MediaRecorder plus a live input level (0..1) from an AnalyserNode, for the record button. */
export function useRecorder() {
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [clip, setClip] = useState<Clip | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const audioCtx = useRef<AudioContext | null>(null);
  const raf = useRef(0);
  const started = useRef(0);

  const teardown = useCallback(() => {
    cancelAnimationFrame(raf.current);
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    void audioCtx.current?.close().catch(() => undefined);
    audioCtx.current = null;
    setLevel(0);
  }, []);

  useEffect(() => {
    if (!recording) return undefined;
    const t = window.setInterval(() => setElapsed(Math.floor((Date.now() - started.current) / 1000)), 250);
    return () => window.clearInterval(t);
  }, [recording]);

  useEffect(
    () => () => {
      if (rec.current && rec.current.state !== 'inactive') rec.current.stop();
      teardown();
    },
    [teardown],
  );

  useEffect(() => {
    const url = clip?.url;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [clip]);

  const start = useCallback(async () => {
    setError(null);
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.current = s;
      const r = new MediaRecorder(s);
      const chunks: Blob[] = [];
      r.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunks.push(ev.data);
      };
      r.onstop = () => {
        const type = r.mimeType || chunks[0]?.type || 'audio/webm';
        const blob = new Blob(chunks, { type });
        const seconds = (Date.now() - started.current) / 1000;
        teardown();
        setRecording(false);
        if (blob.size > 0) setClip({ blob, name: `voice.${extensionFor(type)}`, url: URL.createObjectURL(blob), seconds });
      };
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
        // The level meter is decorative; recording works without it.
      }
      rec.current = r;
      started.current = Date.now();
      setElapsed(0);
      setClip(null);
      r.start();
      setRecording(true);
      haptic([12, 40, 12]);
    } catch (err) {
      teardown();
      setError(`Microphone unavailable: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [teardown]);

  const stop = useCallback(() => {
    haptic(14);
    rec.current?.stop();
    rec.current = null;
  }, []);

  const reset = useCallback(() => setClip(null), []);

  return { recording, elapsed, level, clip, error, start, stop, reset, setClip };
}

export function formatClock(s: number): string {
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

/**
 * The big record control: a rose disc whose halo breathes with the voice level; the inner dot
 * morphs into a rounded stop square while recording.
 */
export function RecordButton({ recording, level, onPress, disabled }: { recording: boolean; level: number; onPress: () => void; disabled?: boolean }) {
  const scale = 1 + level * 0.55;
  return (
    <div className="relative grid h-[168px] w-[168px] place-items-center">
      {recording && (
        <>
          <span className="rec-halo absolute h-[132px] w-[132px] rounded-full bg-danger-400" />
          <span
            aria-hidden="true"
            className="absolute h-[120px] w-[120px] rounded-full bg-danger-400/25 transition-transform duration-[90ms] ease-out"
            style={{ transform: `scale(${scale})` }}
          />
        </>
      )}
      <button
        type="button"
        onClick={onPress}
        disabled={disabled}
        aria-label={recording ? 'Stop recording' : 'Start recording'}
        aria-pressed={recording}
        className={cx(
          'relative grid h-[104px] w-[104px] place-items-center rounded-full transition-[transform,background-color,box-shadow] duration-slow ease-spring active:scale-90 disabled:opacity-40',
          recording ? 'bg-danger-400 shadow-[0_12px_40px_-8px_rgba(255,92,122,0.7)]' : 'bg-ink-800 ring-2 ring-inset ring-danger-400/70',
        )}
      >
        <span
          className={cx(
            'block transition-all duration-slow ease-spring',
            recording ? 'h-8 w-8 rounded-[9px] bg-ink-950' : 'h-11 w-11 rounded-full bg-danger-400',
          )}
        />
      </button>
    </div>
  );
}
