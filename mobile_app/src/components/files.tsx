import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { HubError, type HubClient } from '../lib/api';
import { baseName, displayName, extensionOf, fileKind, isTextual, type FileKind } from '../lib/files';
import { formatBytes } from '../lib/format';
import { errorText, useHub } from '../lib/hub';
import type { StoredObject } from '../lib/types';
import { Icon, type IconName } from './icons';
import { Markdown } from './Markdown';
import { IconButton, Spinner, cx, haptic } from './ui';

export const KIND_STYLE: Record<FileKind, { icon: IconName; tile: string }> = {
  image: { icon: 'eye', tile: 'bg-[#2B4A7A] text-[#A9CBFF]' },
  video: { icon: 'play', tile: 'bg-[#4A2B6E] text-[#D3B2FF]' },
  audio: { icon: 'wave', tile: 'bg-[#5A2340] text-[#FFAFCB]' },
  pdf: { icon: 'file', tile: 'bg-[#5E2A2A] text-[#FFB0A0]' },
  text: { icon: 'note', tile: 'bg-[#2E3A5E] text-[#C9D2F2]' },
  code: { icon: 'table', tile: 'bg-[#1F4A44] text-[#8FE6CF]' },
  sheet: { icon: 'table', tile: 'bg-[#244A2C] text-[#A6E8A0]' },
  doc: { icon: 'book', tile: 'bg-[#2A3D6B] text-[#AFC4FF]' },
  archive: { icon: 'folder', tile: 'bg-[#4F3B1E] text-[#FFD08A]' },
  other: { icon: 'file', tile: 'bg-ink-800 text-ink-300' },
};

// ------------------------------------------------------------------ presigned URLs

const cache = new Map<string, { url: string; until: number }>();

/**
 * A URL that serves `obj` without the hub: a presigned GET when the backend supports it (cached
 * for most of its lifetime), else a blob URL downloaded through the hub (small files only).
 */
export async function objectUrl(client: HubClient, obj: StoredObject, signal?: AbortSignal): Promise<string> {
  const k = `${obj.backend}/${obj.key}`;
  const hit = cache.get(k);
  if (hit && hit.until > Date.now()) return hit.url;
  try {
    const res = await client.drivePresign(obj.key, obj.backend, signal);
    if (res.url) {
      cache.set(k, { url: res.url, until: Date.now() + Math.max((res.expires_s ?? 3600) - 120, 60) * 1000 });
      return res.url;
    }
  } catch (err) {
    if (!(err instanceof HubError && err.status >= 400 && err.status < 500)) throw err;
  }
  const blob = await client.driveDownload(obj.key, obj.backend);
  const url = URL.createObjectURL(blob);
  cache.set(k, { url, until: Date.now() + 30 * 60_000 });
  return url;
}

export function useObjectUrl(obj: StoredObject | null, enabled = true): { url: string | null; error: string | null } {
  const { client } = useHub();
  const [state, setState] = useState<{ url: string | null; error: string | null }>({ url: null, error: null });
  const k = obj ? `${obj.backend}/${obj.key}` : '';
  useEffect(() => {
    if (!obj || !enabled) return undefined;
    const ctrl = new AbortController();
    setState({ url: null, error: null });
    objectUrl(client, obj, ctrl.signal).then(
      (url) => {
        if (!ctrl.signal.aborted) setState({ url, error: null });
      },
      (err) => {
        if (!ctrl.signal.aborted) setState({ url: null, error: errorText(err) });
      },
    );
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, k, enabled]);
  return state;
}

// ------------------------------------------------------------------ icons & thumbnails

export function FileTile({ kind, size = 44, className, fill }: { kind: FileKind; size?: number; className?: string; fill?: boolean }) {
  const s = KIND_STYLE[kind];
  return (
    <span className={cx('grid shrink-0 place-items-center rounded-[12px]', fill && 'aspect-square w-full', s.tile, className)} style={fill ? undefined : { width: size, height: size }}>
      <Icon name={s.icon} size={fill ? 32 : Math.round(size * 0.45)} />
    </span>
  );
}

/** Thumbnail: the image itself (presigned, lazy, cover) once it scrolls near view; a type tile otherwise. */
export function FileThumb({ obj, size = 44, className, fill }: { obj: StoredObject; size?: number; className?: string; fill?: boolean }) {
  const kind = fileKind(obj.key, obj.content_type);
  const ref = useRef<HTMLSpanElement>(null);
  const [near, setNear] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (kind !== 'image' || !ref.current) return undefined;
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true);
      return undefined;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setNear(true);
          io.disconnect();
        }
      },
      { rootMargin: '200px' },
    );
    io.observe(ref.current);
    return () => io.disconnect();
  }, [kind]);
  const { url } = useObjectUrl(obj, kind === 'image' && near);
  if (kind !== 'image' || failed) return <FileTile kind={kind} size={size} className={className} fill={fill} />;
  return (
    <span
      ref={ref}
      className={cx('relative block shrink-0 overflow-hidden rounded-[12px] bg-ink-800', fill && 'aspect-square w-full', className)}
      style={fill ? undefined : { width: size, height: size }}
    >
      {!loaded && <span className="skeleton absolute inset-0 rounded-none" />}
      {url && (
        <img
          src={url}
          alt=""
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          className={cx('h-full w-full object-cover transition-opacity duration-base', loaded ? 'opacity-100' : 'opacity-0')}
        />
      )}
    </span>
  );
}

export function FileName({ objKey, className }: { objKey: string; className?: string }) {
  const ext = extensionOf(objKey);
  return (
    <span className={cx('flex min-w-0 items-center gap-1.5', className)}>
      <span className="truncate text-[15px] text-ink-100" title={baseName(objKey)}>
        {displayName(objKey)}
      </span>
      {ext && <span className="shrink-0 rounded-md bg-ink-800 px-1.5 text-[11px] leading-5 text-ink-400">{ext}</span>}
    </span>
  );
}

// ------------------------------------------------------------------ full-screen viewer

const TEXT_LIMIT = 1_000_000;

function TextPreview({ obj }: { obj: StoredObject }) {
  const { client } = useHub();
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ext = extensionOf(obj.key);
  useEffect(() => {
    let alive = true;
    if (obj.size > TEXT_LIMIT) {
      setError(`Too large to preview (${formatBytes(obj.size)}). Download it instead.`);
      return undefined;
    }
    client.driveDownload(obj.key, obj.backend).then(
      async (blob) => {
        let t = await blob.text();
        if (ext === 'json') {
          try {
            t = JSON.stringify(JSON.parse(t), null, 2);
          } catch {
            // Show it as it is.
          }
        }
        if (alive) setText(t);
      },
      (err) => {
        if (alive) setError(errorText(err));
      },
    );
    return () => {
      alive = false;
    };
  }, [client, obj, ext]);
  if (error) return <p className="p-6 text-center text-[15px] text-ink-300">{error}</p>;
  if (text === null) return <div className="grid h-full place-items-center"><Spinner className="h-6 w-6" /></div>;
  if (ext === 'md' || ext === 'markdown') {
    return (
      <div className="h-full overflow-y-auto bg-ink-900 px-5 py-6">
        <Markdown text={text} className="mx-auto max-w-prose" />
      </div>
    );
  }
  return (
    <pre className="h-full overflow-auto whitespace-pre-wrap break-words bg-ink-900 p-5 font-mono text-[13px] leading-relaxed text-ink-200">
      {text.split('\n').map((line, i) => (
        <span key={i} className="block">
          <span className="mr-4 inline-block w-8 select-none text-right text-ink-600">{i + 1}</span>
          {line}
        </span>
      ))}
    </pre>
  );
}

/** Image with pinch-zoom, double-tap zoom and pan; horizontal swipes go to the neighbours. */
function ZoomImage({ obj, onSwipe }: { obj: StoredObject; onSwipe: (dir: -1 | 1) => void }) {
  const { url, error } = useObjectUrl(obj);
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
    apply(false);
  }, [obj.key]);

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
      } else {
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
      if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy)) onSwipe(dx < 0 ? 1 : -1);
      t.current = { scale: 1, x: 0, y: 0 };
      apply(true);
    }
  };

  if (error) return <p className="grid h-full place-items-center p-6 text-center text-ink-300">{error}</p>;
  return (
    <div className="relative grid h-full w-full touch-none place-items-center overflow-hidden" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
      {!url && <Spinner className="h-6 w-6" />}
      {url && <img ref={imgRef} src={url} alt={displayName(obj.key)} draggable={false} className="screen-in-up max-h-full max-w-full select-none object-contain" />}
    </div>
  );
}

function MediaPreview({ obj, kind }: { obj: StoredObject; kind: FileKind }) {
  const { url, error } = useObjectUrl(obj);
  if (error) return <p className="grid h-full place-items-center p-6 text-center text-ink-300">{error}</p>;
  if (!url) return <div className="grid h-full place-items-center"><Spinner className="h-6 w-6" /></div>;
  if (kind === 'video') return <video src={url} controls playsInline autoPlay className="h-full w-full bg-black object-contain" />;
  if (kind === 'audio') {
    return (
      <div className="grid h-full place-items-center p-6">
        <div className="w-full max-w-sm space-y-6 text-center">
          <FileTile kind="audio" size={112} className="mx-auto rounded-[28px]" />
          <audio src={url} controls autoPlay className="w-full" />
        </div>
      </div>
    );
  }
  if (kind === 'pdf') return <iframe src={url} title={displayName(obj.key)} className="h-full w-full bg-white" />;
  return null;
}

export function FileViewer({
  items,
  index,
  onIndex,
  onClose,
  onActions,
}: {
  items: StoredObject[];
  index: number;
  onIndex: (i: number) => void;
  onClose: () => void;
  onActions: (obj: StoredObject) => void;
}) {
  const obj = items[index];
  const [dir, setDir] = useState(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'ArrowRight' && index < items.length - 1) onIndex(index + 1);
      if (e.key === 'ArrowLeft' && index > 0) onIndex(index - 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [index, items.length, onClose, onIndex]);
  if (!obj) return null;
  const kind = fileKind(obj.key, obj.content_type);
  const step = (d: -1 | 1) => {
    const next = index + d;
    if (next < 0 || next >= items.length) return;
    haptic(5);
    setDir(d);
    onIndex(next);
  };
  const images = items.filter((o) => fileKind(o.key, o.content_type) === 'image');
  const imgPos = images.indexOf(obj);
  return (
    <div className="backdrop-enter fixed inset-0 z-[45] flex flex-col bg-[#05060f]" role="dialog" aria-modal="true" aria-label={`Preview ${displayName(obj.key)}`}>
      <header className="pt-safe flex shrink-0 items-center gap-1 bg-gradient-to-b from-black/60 to-transparent px-2 pb-2">
        <IconButton icon="left" label="Close preview" onClick={onClose} />
        <div className="min-w-0 flex-1">
          <FileName objKey={obj.key} />
          <p className="text-[12px] text-ink-400">
            {formatBytes(obj.size)} on {obj.backend}
            {imgPos >= 0 && images.length > 1 ? `, ${imgPos + 1} of ${images.length}` : ''}
          </p>
        </div>
        <IconButton icon="more" label="File actions" onClick={() => onActions(obj)} />
      </header>
      <div key={obj.key} className={cx('min-h-0 flex-1', dir > 0 ? 'screen-in-right' : dir < 0 ? 'screen-in-left' : 'screen-in-up')}>
        {kind === 'image' ? (
          <ZoomImage obj={obj} onSwipe={(d) => step(d)} />
        ) : isTextual(kind) ? (
          <TextPreview obj={obj} />
        ) : kind === 'video' || kind === 'audio' || kind === 'pdf' ? (
          <MediaPreview obj={obj} kind={kind} />
        ) : (
          <div className="grid h-full place-items-center p-8 text-center">
            <div className="space-y-4">
              <FileTile kind={kind} size={96} className="mx-auto rounded-[24px]" />
              <p className="text-[15px] text-ink-300">No preview for this file type.</p>
              <button type="button" onClick={() => onActions(obj)} className="press min-h-[44px] rounded-full bg-signal px-5 font-semibold text-ink-950">
                Download or share
              </button>
            </div>
          </div>
        )}
      </div>
      {items.length > 1 && kind !== 'image' && (
        <div className="pb-safe flex shrink-0 justify-between px-3 py-2">
          <IconButton icon="left" label="Previous file" onClick={() => step(-1)} disabled={index === 0} />
          <IconButton icon="right" label="Next file" onClick={() => step(1)} disabled={index === items.length - 1} />
        </div>
      )}
    </div>
  );
}
