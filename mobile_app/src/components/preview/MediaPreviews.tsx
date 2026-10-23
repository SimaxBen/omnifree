// Images (pinch/double-tap zoom, swipe to neighbours), video and audio. Audio files in the Drive
// can be transcribed into a note by the hub (POST /drive/transcribe).

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { useActions } from '../../lib/actions';
import { displayName } from '../../lib/files';
import { formatBytes } from '../../lib/format';
import { errorText, useHub } from '../../lib/hub';
import type { PreviewSource } from '../../lib/preview/source';
import type { Note } from '../../lib/types';
import { Icon } from '../icons';
import { Spinner, cx } from '../ui';
import { PreviewError, PreviewMessage } from './common';

function useSourceUrl(src: PreviewSource): { url: string | null; error: unknown } {
  const [state, setState] = useState<{ url: string | null; error: unknown }>({ url: null, error: null });
  useEffect(() => {
    const ctrl = new AbortController();
    setState({ url: null, error: null });
    src.url(ctrl.signal).then(
      (url) => !ctrl.signal.aborted && setState({ url, error: null }),
      (error) => !ctrl.signal.aborted && setState({ url: null, error }),
    );
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src.id]);
  return state;
}

/** Image with pinch-zoom, double-tap zoom and pan; horizontal swipes go to the neighbours. */
export function ZoomImage({ src, onSwipe }: { src: PreviewSource; onSwipe?: (dir: -1 | 1) => void }) {
  const { url, error } = useSourceUrl(src);
  const [broken, setBroken] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const t = useRef({ scale: 1, x: 0, y: 0 });
  const pts = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef<{ dist: number; scale: number; sx: number; sy: number; x: number; y: number; t: number } | null>(null);
  const lastTap = useRef(0);

  const apply = (animate: boolean) => {
    const el = imgRef.current;
    if (!el) return;
    el.style.transition = animate ? 'transform 260ms var(--ease-out)' : 'none';
    el.style.transform = `translate3d(${t.current.x}px, ${t.current.y}px, 0) scale(${t.current.scale})`;
  };
  useEffect(() => {
    t.current = { scale: 1, x: 0, y: 0 };
    setBroken(false);
    apply(false);
  }, [src.id]);

  const down = (e: ReactPointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    pts.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const p = [...pts.current.values()];
    gesture.current = {
      dist: p.length > 1 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0,
      scale: t.current.scale,
      sx: e.clientX,
      sy: e.clientY,
      x: t.current.x,
      y: t.current.y,
      t: performance.now(),
    };
  };
  const move = (e: ReactPointerEvent) => {
    if (!pts.current.has(e.pointerId) || !gesture.current) return;
    pts.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const p = [...pts.current.values()];
    const g = gesture.current;
    if (p.length > 1 && g.dist) {
      const d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      t.current.scale = Math.min(5, Math.max(1, (g.scale * d) / g.dist));
    } else {
      const dx = e.clientX - g.sx;
      const dy = e.clientY - g.sy;
      if (t.current.scale > 1) {
        t.current.x = g.x + dx;
        t.current.y = g.y + dy;
      } else if (onSwipe) {
        t.current.x = dx;
      }
    }
    apply(false);
  };
  const up = (e: ReactPointerEvent) => {
    const g = gesture.current;
    pts.current.delete(e.pointerId);
    if (pts.current.size > 0) return;
    gesture.current = null;
    if (!g) return;
    const dx = e.clientX - g.sx;
    const dy = e.clientY - g.sy;
    const quick = performance.now() - g.t < 250 && Math.abs(dx) < 10 && Math.abs(dy) < 10;
    if (quick) {
      const now = performance.now();
      if (now - lastTap.current < 300) {
        t.current = t.current.scale > 1 ? { scale: 1, x: 0, y: 0 } : { scale: 2.5, x: 0, y: 0 };
        apply(true);
        lastTap.current = 0;
        return;
      }
      lastTap.current = now;
    }
    if (t.current.scale <= 1.01) {
      if (onSwipe && Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy)) onSwipe(dx < 0 ? 1 : -1);
      t.current = { scale: 1, x: 0, y: 0 };
      apply(true);
    }
  };

  if (error) return <PreviewError src={src} error={error} />;
  if (broken) {
    return (
      <PreviewMessage src={src} icon="eye" title="This image can't be shown here">
        The browser can't decode this format (HEIC photos, for example). Open it in a photo app instead.
      </PreviewMessage>
    );
  }
  return (
    <div className="relative grid h-full w-full touch-none place-items-center overflow-hidden" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
      {!url && <Spinner className="h-6 w-6" />}
      {/* SVG files stay in <img> (a blob or presigned URL): scripts inside them never run. */}
      {url && (
        <img
          ref={imgRef}
          src={url}
          alt={displayName(src.path)}
          draggable={false}
          onError={() => setBroken(true)}
          className="screen-in-up max-h-full max-w-full select-none object-contain"
        />
      )}
    </div>
  );
}

const LANGUAGES: { value: string; label: string }[] = [
  { value: '', label: 'Auto' },
  { value: 'fr', label: 'French' },
  { value: 'en', label: 'English' },
  { value: 'ar', label: 'Arabic' },
  { value: 'es', label: 'Spanish' },
];

const WHISPER_MAX = 25 * 1024 * 1024;

function TranscribePanel({ src, onClose }: { src: PreviewSource; onClose?: () => void }) {
  const { client, notify } = useHub();
  const actions = useActions();
  const [language, setLanguage] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ note: Note; transcript: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const obj = src.obj!;
  if (obj.size > WHISPER_MAX) {
    return <p className="text-center text-[13px] text-ink-500">Transcription takes audio up to 25 MB; this file is {formatBytes(obj.size)}.</p>;
  }
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await client.driveTranscribe({ provider: obj.backend, key: obj.key, language: language || undefined });
      setResult(res);
      notify('Transcript saved as a note', 'ok');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  if (result) {
    return (
      <div className="space-y-3 rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800">
        <p className="flex items-center gap-2 text-[14px] font-semibold text-mint-400">
          <Icon name="check" size={16} /> Saved to {result.note.notebook}
        </p>
        <p className="line-clamp-5 whitespace-pre-wrap text-[14px] leading-relaxed text-ink-200">{result.transcript}</p>
        <button
          type="button"
          onClick={() => {
            onClose?.();
            actions.openNote(result.note);
          }}
          className="press flex min-h-[44px] w-full items-center justify-center gap-2 rounded-full bg-ink-800 text-[15px] text-ink-100"
        >
          <Icon name="note" size={18} /> Open note
        </button>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label className="sr-only" htmlFor="tr-lang">
          Spoken language
        </label>
        <select
          id="tr-lang"
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          disabled={busy}
          className="min-h-[48px] rounded-full bg-ink-850 px-4 text-[14px] text-ink-100 ring-1 ring-inset ring-ink-700"
        >
          {LANGUAGES.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={run}
          disabled={busy}
          className="press flex min-h-[48px] flex-1 items-center justify-center gap-2 rounded-full bg-signal px-4 text-[15px] font-semibold text-ink-950 disabled:opacity-60"
        >
          {busy ? <Spinner className="border-ink-950/30 border-t-ink-950" /> : <Icon name="note" size={18} />}
          {busy ? 'Transcribing…' : 'Transcribe to note'}
        </button>
      </div>
      {busy && <p className="text-center text-[12px] text-ink-500">Up to a minute for long recordings.</p>}
      {error && <p className="text-center text-[13px] text-danger-400">{error}</p>}
    </div>
  );
}

export function MediaPreview({ src, kind, onClose }: { src: PreviewSource; kind: 'video' | 'audio'; onClose?: () => void }) {
  const { url, error } = useSourceUrl(src);
  const [broken, setBroken] = useState(false);
  if (error) return <PreviewError src={src} error={error} />;
  if (broken) {
    return (
      <PreviewMessage src={src} icon={kind === 'video' ? 'play' : 'wave'} title="This format doesn't play here">
        The browser can't play this file. Open it in a media player instead.
      </PreviewMessage>
    );
  }
  if (!url) {
    return (
      <div className="grid h-full place-items-center">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (kind === 'video') return <video src={url} controls playsInline autoPlay onError={() => setBroken(true)} className="h-full w-full bg-black object-contain" />;
  return (
    <div className="grid h-full place-items-center overflow-y-auto p-6">
      <div className="w-full max-w-sm space-y-6 text-center">
        <span className={cx('mx-auto grid h-28 w-28 place-items-center rounded-[28px] bg-[#5A2340] text-[#FFAFCB]')}>
          <Icon name="wave" size={50} />
        </span>
        <audio src={url} controls autoPlay onError={() => setBroken(true)} className="w-full" />
        {src.obj && <TranscribePanel src={src} onClose={onClose} />}
      </div>
    </div>
  );
}
