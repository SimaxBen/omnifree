import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { baseName, displayName, extensionOf, fileKind } from '../lib/files';
import { formatBytes } from '../lib/format';
import { errorText, useHub } from '../lib/hub';
import { useOverlayBack } from '../lib/overlay';
import { driveSource, objectUrl } from '../lib/preview/source';
import { cachedThumb, getThumb, thumbSourceOf, type Thumb } from '../lib/thumbs';
import type { StoredObject } from '../lib/types';
import { FileTile, PaperTile } from './fileTile';
import { Icon } from './icons';
import { PreviewBody } from './preview/PreviewBody';
import { IconButton, cx, haptic } from './ui';

export { FileTile, KIND_STYLE } from './fileTile';
export { objectUrl } from '../lib/preview/source';

// ------------------------------------------------------------------ presigned URLs

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

// ------------------------------------------------------------------ thumbnails

/**
 * `near`: the element is within `margin` of the viewport right now (thumbnail jobs follow it, so a
 * fast fling never leaves a queue of off-screen tiles ahead of the visible ones); `seen`: it has
 * been near at least once (then stays true).
 */
function useNear(ref: RefObject<HTMLElement | null>, enabled: boolean, margin = '200px'): { near: boolean; seen: boolean } {
  const [near, setNear] = useState(false);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    if (!enabled || !ref.current) return undefined;
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true);
      setSeen(true);
      return undefined;
    }
    const io = new IntersectionObserver(
      (entries) => {
        const on = entries[entries.length - 1].isIntersecting;
        setNear(on);
        if (on) setSeen(true);
      },
      { rootMargin: margin },
    );
    io.observe(ref.current);
    return () => io.disconnect();
  }, [ref, enabled, margin]);
  return { near, seen };
}

/**
 * Thumbnail: a small generated picture (images, video frames, PDF page 1, Office embedded
 * thumbnails; cached in IndexedDB) once the item scrolls near view, a document's first words on a
 * paper tile, or the type tile. SVGs (and images on a bucket without CORS) load the file itself.
 */
export function FileThumb({ obj, size = 44, className, fill }: { obj: StoredObject; size?: number; className?: string; fill?: boolean }) {
  const { client } = useHub();
  const kind = fileKind(obj.key, obj.content_type);
  const generated = thumbSourceOf(obj) !== null;
  const svg = kind === 'image' && extensionOf(obj.key) === 'svg' && obj.size <= 5 * 1024 * 1024;
  const ref = useRef<HTMLSpanElement>(null);
  const { near, seen } = useNear(ref, generated || svg);
  const [thumb, setThumb] = useState<Thumb | undefined>(() => cachedThumb(obj));
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const id = `${obj.backend}/${obj.key}/${obj.etag ?? obj.last_modified ?? ''}/${obj.size}`;

  useEffect(() => {
    setThumb(cachedThumb(obj));
    setLoaded(false);
    setFailed(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!near || !generated || thumb) return undefined;
    const ctrl = new AbortController();
    getThumb(client, obj, ctrl.signal).then(
      (t) => !ctrl.signal.aborted && setThumb(t),
      () => undefined,
    );
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [near, generated, id, client, thumb]);

  const direct = svg || (kind === 'image' && thumb?.kind === 'none' && thumb.cors);
  const { url: directUrl } = useObjectUrl(obj, !!direct && seen);
  const src = thumb?.kind === 'image' ? thumb.url : direct ? directUrl : null;
  const box = cx('relative block shrink-0 overflow-hidden rounded-[12px]', fill && 'aspect-square w-full', className);
  const style = fill ? undefined : { width: size, height: size };

  let inner;
  if (src && !failed) {
    inner = (
      <>
        {!loaded && <span className="skeleton absolute inset-0 rounded-none" />}
        <img
          src={src}
          alt=""
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          className={cx('h-full w-full transition-opacity duration-base', kind === 'pdf' || kind === 'doc' || kind === 'slides' ? 'bg-white object-cover object-top' : 'object-cover', loaded ? 'opacity-100' : 'opacity-0')}
        />
        {kind === 'video' && loaded && (
          <span className="absolute bottom-1 right-1 grid h-5 w-5 place-items-center rounded-full bg-black/60 text-white">
            <Icon name="play" size={11} />
          </span>
        )}
      </>
    );
  } else if (thumb?.kind === 'text' && thumb.text) {
    return (
      <span ref={ref} className={cx('block shrink-0', fill && 'w-full')}>
        <PaperTile kind={kind} text={thumb.text} size={size} fill={fill} className={className} />
      </span>
    );
  } else if (kind === 'image' && !failed && (direct || (generated && !thumb))) {
    inner = <span className="skeleton absolute inset-0 rounded-none" />;
  } else {
    return (
      <span ref={ref} className={cx('block shrink-0', fill && 'w-full')}>
        <FileTile kind={kind} size={size} className={className} fill={fill} />
      </span>
    );
  }
  return (
    <span ref={ref} className={cx(box, 'bg-ink-800')} style={style}>
      {inner}
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
  const { client } = useHub();
  const obj = items[index];
  const [dir, setDir] = useState(0);
  // The Android back gesture closes the viewer (a file opened inside a zip closes first).
  const close = useOverlayBack(true, onClose);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
      if (e.key === 'ArrowRight' && index < items.length - 1) onIndex(index + 1);
      if (e.key === 'ArrowLeft' && index > 0) onIndex(index - 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [index, items.length, close, onIndex]);
  const src = useMemo(() => (obj ? driveSource(client, obj) : null), [client, obj]);
  if (!obj || !src) return null;
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
        <IconButton icon="left" label="Close preview" onClick={() => close()} />
        <div className="min-w-0 flex-1">
          <FileName objKey={obj.key} />
          <p className="text-[12px] text-ink-400">
            {formatBytes(obj.size)} on {obj.backend}
            {imgPos >= 0 && images.length > 1 ? `, ${imgPos + 1} of ${images.length}` : ''}
          </p>
        </div>
        <IconButton icon="more" label="File actions" onClick={() => onActions(obj)} />
      </header>
      <div key={obj.key} className={cx('relative min-h-0 flex-1', dir > 0 ? 'screen-in-right' : dir < 0 ? 'screen-in-left' : 'screen-in-up')}>
        <PreviewBody src={src} onSwipe={kind === 'image' ? (d) => step(d) : undefined} onClose={close} />
      </div>
      {items.length > 1 && kind !== 'image' && (
        <div className="pb-safe flex shrink-0 justify-between px-3 py-1">
          <IconButton icon="left" label="Previous file" onClick={() => step(-1)} disabled={index === 0} />
          <IconButton icon="right" label="Next file" onClick={() => step(1)} disabled={index === items.length - 1} />
        </div>
      )}
    </div>
  );
}
