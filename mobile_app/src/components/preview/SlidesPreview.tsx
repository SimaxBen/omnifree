// PowerPoint (.pptx) as text and pictures, slide by slide (swipe or arrows). Not a faithful layout:
// the deck's own thumbnail (first slide as PowerPoint rendered it) is shown when the file has one.

import JSZip from 'jszip';
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { imageMime } from '../../lib/preview/ooxml';
import { parsePptx, type PptxDeck } from '../../lib/preview/pptx';
import { ZipCapError, extractEntry } from '../../lib/preview/zip';
import type { PreviewSource } from '../../lib/preview/source';
import { IconButton, cx } from '../ui';
import { Loading, PreviewError, useSourceBytes } from './common';

export const SLIDES_MAX_BYTES = 60 * 1024 * 1024;
const IMAGE_MAX_BYTES = 15 * 1024 * 1024;

/** Blob URLs for a slide's pictures, created when the slide shows and revoked after. */
function useZipImages(zip: JSZip | null, paths: string[]): Map<string, string> {
  const [urls, setUrls] = useState<Map<string, string>>(new Map());
  const key = paths.join('|');
  useEffect(() => {
    if (!zip || !paths.length) {
      setUrls(new Map());
      return undefined;
    }
    let alive = true;
    const made: string[] = [];
    (async () => {
      const out = new Map<string, string>();
      for (const p of paths) {
        if (!zip.file(p)) continue;
        // Capped while inflating: a picture that lies about its size stops at the limit.
        const bytes = await extractEntry(zip, p, IMAGE_MAX_BYTES).catch(() => null);
        if (!alive) return;
        if (!bytes) continue;
        const url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: imageMime(p) }));
        made.push(url);
        out.set(p, url);
      }
      if (alive) setUrls(out);
    })().catch(() => undefined);
    return () => {
      alive = false;
      for (const u of made) URL.revokeObjectURL(u);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zip, key]);
  return urls;
}

export default function SlidesPreview({ src }: { src: PreviewSource }) {
  const { data, error: loadError } = useSourceBytes(src, SLIDES_MAX_BYTES);
  const [deck, setDeck] = useState<{ zip: JSZip; deck: PptxDeck } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [i, setI] = useState(0);
  const swipe = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!data) return undefined;
    let alive = true;
    setDeck(null);
    setError(null);
    setI(0);
    JSZip.loadAsync(data)
      .then(async (zip) => ({ zip, deck: await parsePptx(zip) }))
      .then(
        (d) => alive && setDeck(d),
        (err) => alive && setError(!(err instanceof ZipCapError) && /zip|central directory|end of data/i.test(String(err)) ? "This doesn't look like a valid .pptx file (damaged or password-protected)." : err),
      );
    return () => {
      alive = false;
    };
  }, [data]);

  const slide = deck?.deck.slides[i];
  const paths = slide ? (i === 0 && deck?.deck.thumbnail ? [deck.deck.thumbnail, ...slide.images] : slide.images) : [];
  const urls = useZipImages(deck?.zip ?? null, paths);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'PageDown') setI((v) => Math.min(v + 1, (deck?.deck.slides.length ?? 1) - 1));
      if (e.key === 'PageUp') setI((v) => Math.max(v - 1, 0));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [deck]);

  if (loadError || error) return <PreviewError src={src} error={loadError || error} />;
  if (!deck) return <Loading label={data ? 'Reading the slides' : 'Downloading the presentation'} />;
  const n = deck.deck.slides.length;
  if (!n || !slide) return <PreviewError src={src} error="This presentation has no slides." />;
  const go = (d: number) => setI((v) => Math.min(Math.max(v + d, 0), n - 1));
  const down = (e: ReactPointerEvent) => {
    swipe.current = { x: e.clientX, y: e.clientY };
  };
  const up = (e: ReactPointerEvent) => {
    const s = swipe.current;
    swipe.current = null;
    if (!s) return;
    const dx = e.clientX - s.x;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(e.clientY - s.y) * 1.5) go(dx < 0 ? 1 : -1);
  };
  const thumb = i === 0 && deck.deck.thumbnail ? urls.get(deck.deck.thumbnail) : undefined;

  return (
    <div className="flex h-full flex-col bg-[#1b1d2b]">
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-4" onPointerDown={down} onPointerUp={up} style={{ touchAction: 'pan-y' }}>
        <article key={i} className="screen-in-up mx-auto max-w-[720px] space-y-4 rounded-[10px] bg-[#FBFAF6] p-5 text-[#1d1d24] shadow-[0_2px_14px_rgba(0,0,0,0.45)]">
          {thumb && <img src={thumb} alt="First slide as saved by PowerPoint" className="w-full rounded-[6px] ring-1 ring-black/10" />}
          <h2 className="font-display text-[21px] font-semibold leading-tight">{slide.title || `Slide ${slide.number}`}</h2>
          {slide.paragraphs.length > 0 && (
            <ul className="space-y-1.5 text-[15px] leading-relaxed">
              {slide.paragraphs.map((p, k) => (
                <li key={k} className="flex gap-2" style={{ paddingLeft: p.level * 16 }}>
                  <span aria-hidden="true" className="select-none text-[#8a8aa0]">
                    {p.level ? '–' : '•'}
                  </span>
                  <span className="min-w-0 whitespace-pre-wrap break-words">{p.text}</span>
                </li>
              ))}
            </ul>
          )}
          {slide.images
            .map((p) => urls.get(p))
            .filter((u): u is string => !!u)
            .map((u) => (
              <img key={u} src={u} alt="" loading="lazy" className="mx-auto max-h-[60vh] rounded-[4px]" />
            ))}
          {!slide.title && !slide.paragraphs.length && !slide.images.length && !thumb && <p className="text-[14px] text-[#6a6a80]">No text or pictures on this slide.</p>}
        </article>
      </div>
      <div className="pb-safe flex shrink-0 items-center justify-between gap-2 border-t border-ink-800 bg-ink-900 px-2 py-1.5">
        <IconButton icon="left" label="Previous slide" onClick={() => go(-1)} disabled={i === 0} />
        <span className="text-[14px] font-semibold tabular-nums text-ink-100" aria-live="polite">
          Slide {i + 1} / {n}
        </span>
        <IconButton icon="right" label="Next slide" onClick={() => go(1)} disabled={i === n - 1} className={cx(i < n - 1 && 'text-signal')} />
      </div>
    </div>
  );
}
