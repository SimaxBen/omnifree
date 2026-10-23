// PDF viewer on pdf.js (phones have no built-in PDF iframe): continuous scroll, pages rendered
// only around the viewport (visible +/- 1, others release their canvas), fit-to-width, pinch and
// button zoom, "3 / 12" indicator, text search with highlights, password prompt.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type MouseEvent as ReactMouseEvent } from 'react';
import { PdfError, Util, openPdf, outputScale, type PDFDocumentProxy, type TextItem } from '../../lib/preview/pdfjs';
import { findInPages, type PdfMatch, type PdfTextItem } from '../../lib/preview/pdfSearch';
import type { PreviewSource } from '../../lib/preview/source';
import { Icon } from '../icons';
import { IconButton, Spinner, cx, inputClass } from '../ui';
import { FallbackActions, Loading, PreviewError, PreviewMessage, useSourceBytes } from './common';

export const PDF_MAX_BYTES = 80 * 1024 * 1024;
const MAX_PAGES = 2000;
const GAP = 12;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 4;
const NO_MATCHES: PdfMatch[] = [];

interface Size {
  w: number;
  h: number;
}

interface Highlight {
  left: number;
  top: number;
  width: number;
  height: number;
  current: boolean;
}

function PdfPage({
  doc,
  index,
  width,
  height,
  scale,
  active,
  textItems,
  matches,
  currentMatch,
}: {
  doc: PDFDocumentProxy;
  index: number;
  width: number;
  height: number;
  scale: number;
  active: boolean;
  textItems: TextItem[] | undefined;
  matches: PdfMatch[];
  currentMatch: PdfMatch | null;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const [baseTransform, setBaseTransform] = useState<number[] | null>(null);

  useEffect(() => {
    const el = holder.current;
    if (!active || !el) {
      // Off-screen: give the canvas memory back, and let pdf.js drop the page's decoded resources.
      const canvases = Array.from(el?.querySelectorAll('canvas') ?? []);
      for (const c of canvases) {
        c.width = 0;
        c.height = 0;
        c.remove();
      }
      if (canvases.length) {
        doc.getPage(index + 1).then(
          (page) => page.cleanup(),
          () => undefined,
        );
      }
      return undefined;
    }
    let cancelled = false;
    let task: { cancel: () => void } | null = null;
    // Re-render after zoom settles; the old canvas stays (stretched) meanwhile.
    const timer = window.setTimeout(async () => {
      try {
        const page = await doc.getPage(index + 1);
        if (cancelled) return;
        const viewport = page.getViewport({ scale });
        const out = outputScale(viewport.width, viewport.height);
        const canvas = document.createElement('canvas');
        canvas.width = Math.floor(viewport.width * out);
        canvas.height = Math.floor(viewport.height * out);
        canvas.className = 'absolute inset-0 h-full w-full';
        const render = page.render({
          canvas,
          viewport,
          transform: out !== 1 ? [out, 0, 0, out, 0, 0] : undefined,
        });
        task = render;
        await render.promise;
        if (cancelled) {
          canvas.width = 0;
          return;
        }
        for (const old of Array.from(el.querySelectorAll('canvas'))) {
          old.width = 0;
          old.height = 0;
          old.remove();
        }
        el.prepend(canvas);
        setBaseTransform(page.getViewport({ scale: 1 }).transform);
        setFailed(false);
      } catch (err) {
        if (!cancelled && !(err instanceof Error && err.name === 'RenderingCancelledException')) setFailed(true);
      }
    }, 60);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      task?.cancel();
    };
  }, [doc, index, scale, active]);

  const highlights = useMemo<Highlight[]>(() => {
    if (!textItems || !baseTransform || !matches.length) return [];
    // A viewport's transform is linear in its scale: scale the 1x one to the current zoom.
    const vt = baseTransform.map((v) => v * scale);
    const out: Highlight[] = [];
    for (const m of matches) {
      for (const s of m.segments) {
        const item = textItems[s.item];
        if (!item || !item.str) continue;
        const tx = Util.transform(vt, item.transform);
        const fontHeight = Math.hypot(tx[2], tx[3]);
        const w = item.width * scale;
        const len = item.str.length;
        out.push({
          left: tx[4] + (w * s.start) / len,
          top: tx[5] - fontHeight,
          width: Math.max(2, (w * (s.end - s.start)) / len),
          height: fontHeight * 1.2,
          current: m === currentMatch,
        });
      }
    }
    return out;
  }, [textItems, baseTransform, matches, currentMatch, scale]);

  return (
    <div
      ref={holder}
      data-page={index}
      className="relative mx-auto overflow-hidden bg-white shadow-[0_2px_14px_rgba(0,0,0,0.45)]"
      style={{ width, height }}
      aria-label={`Page ${index + 1}`}
    >
      {failed && <p className="absolute inset-0 grid place-items-center text-[13px] text-neutral-500">This page couldn't be drawn.</p>}
      {highlights.map((h, i) => (
        <span
          key={i}
          className={cx('pointer-events-none absolute rounded-[2px] mix-blend-multiply', h.current ? 'bg-[#FF9548]/70' : 'bg-[#FFE066]/60')}
          style={{ left: h.left, top: h.top, width: h.width, height: h.height }}
        />
      ))}
    </div>
  );
}

function PasswordPrompt({ wrong, onSubmit, src }: { wrong: boolean; onSubmit: (pw: string) => void; src: PreviewSource }) {
  const [pw, setPw] = useState('');
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (pw) onSubmit(pw);
  };
  return (
    <div className="grid h-full place-items-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 text-center">
        <span className="mx-auto grid h-20 w-20 place-items-center rounded-[22px] bg-ink-850 text-ink-300 ring-1 ring-inset ring-ink-700">
          <Icon name="lock" size={34} />
        </span>
        <p className="font-display text-[18px] font-semibold text-ink-100">This PDF is password-protected</p>
        <input
          type="password"
          autoComplete="off"
          aria-label="PDF password"
          placeholder="Password"
          className={inputClass}
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
        {wrong && <p className="text-[13px] text-danger-400">Wrong password, try again.</p>}
        <button type="submit" className="press min-h-[44px] w-full rounded-full bg-signal font-semibold text-ink-950">
          Open
        </button>
        <FallbackActions src={src} />
      </form>
    </div>
  );
}

export default function PdfViewer({ src }: { src: PreviewSource }) {
  const { data, error: loadError } = useSourceBytes(src, PDF_MAX_BYTES);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [openError, setOpenError] = useState<PdfError | null>(null);
  const [opening, setOpening] = useState(false);
  const [sizes, setSizes] = useState<Size[]>([]);
  const [width, setWidth] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [range, setRange] = useState<[number, number]>([0, 1]);
  const [current, setCurrent] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef<{ left: number; top: number } | null>(null);

  // Search.
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [texts, setTexts] = useState<Map<number, TextItem[]>>(new Map());
  const [matches, setMatches] = useState<PdfMatch[] | null>(null);
  const [matchIndex, setMatchIndex] = useState(0);
  const [searching, setSearching] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const open = useCallback(
    async (password?: string) => {
      if (!data) return;
      setOpening(true);
      try {
        // openPdf hands the bytes to the worker (no second copy of a large file); a retry with a
        // password fetches them again.
        const bytes = data.byteLength ? data : await src.bytes(PDF_MAX_BYTES);
        const d = await openPdf(bytes, password);
        if (!alive.current) {
          // Closed while opening: nobody will destroy this document otherwise.
          void d.loadingTask.destroy();
          return;
        }
        setOpenError(null);
        setDoc(d);
      } catch (err) {
        if (alive.current) setOpenError(err instanceof PdfError ? err : new PdfError('other', String(err)));
      } finally {
        if (alive.current) setOpening(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data],
  );

  useEffect(() => {
    void open();
  }, [open]);

  useEffect(
    () => () => {
      void doc?.loadingTask.destroy();
    },
    [doc],
  );

  // Page sizes: the first page right away, the others in the background (batched updates).
  useEffect(() => {
    if (!doc) return undefined;
    let alive = true;
    const n = Math.min(doc.numPages, MAX_PAGES);
    (async () => {
      const first = (await doc.getPage(1)).getViewport({ scale: 1 });
      if (!alive) return;
      const all: Size[] = Array.from({ length: n }, () => ({ w: first.width, h: first.height }));
      setSizes([...all]);
      for (let i = 1; i < n; i++) {
        const v = (await doc.getPage(i + 1)).getViewport({ scale: 1 });
        if (!alive) return;
        all[i] = { w: v.width, h: v.height };
        if (i % 25 === 0 || i === n - 1) setSizes([...all]);
      }
    })().catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [doc]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return undefined;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [doc, sizes.length > 0]);

  const fitWidth = Math.max(100, width - GAP * 2);
  const layout = useMemo(() => {
    const pages = sizes.map((s) => {
      const scale = (fitWidth / s.w) * zoom;
      return { scale, w: Math.floor(s.w * scale), h: Math.floor(s.h * scale) };
    });
    const tops: number[] = [];
    let y = GAP;
    for (const p of pages) {
      tops.push(y);
      y += p.h + GAP;
    }
    const contentWidth = Math.max(width, ...pages.map((p) => p.w + GAP * 2));
    return { pages, tops, height: y, contentWidth };
  }, [sizes, fitWidth, zoom, width]);

  const updateRange = useCallback(() => {
    const el = scroller.current;
    if (!el || !layout.pages.length) return;
    const top = el.scrollTop;
    const bottom = top + el.clientHeight;
    const { tops, pages } = layout;
    let first = 0;
    while (first < pages.length - 1 && tops[first] + pages[first].h < top) first++;
    let last = first;
    while (last < pages.length - 1 && tops[last + 1] < bottom) last++;
    setRange((r) => (r[0] === Math.max(0, first - 1) && r[1] === Math.min(pages.length - 1, last + 1) ? r : [Math.max(0, first - 1), Math.min(pages.length - 1, last + 1)]));
    const probe = top + el.clientHeight * 0.3;
    let cur = 0;
    while (cur < pages.length - 1 && tops[cur + 1] <= probe) cur++;
    setCurrent(cur);
  }, [layout]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pendingScroll.current) {
      el.scrollLeft = pendingScroll.current.left;
      el.scrollTop = pendingScroll.current.top;
      pendingScroll.current = null;
    }
    updateRange();
  }, [layout, updateRange]);

  const frame = useRef(0);
  const onScroll = () => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(updateRange);
  };

  /** Zoom keeping the content point at viewport (cx, cy) under the same spot. */
  const zoomTo = useCallback(
    (next: number, cx?: number, cy?: number) => {
      const el = scroller.current;
      if (!el) return;
      const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next));
      if (Math.abs(z - zoom) < 0.001) return;
      const r = z / zoom;
      const px = cx ?? el.clientWidth / 2;
      const py = cy ?? el.clientHeight / 2;
      pendingScroll.current = { left: (el.scrollLeft + px) * r - px, top: (el.scrollTop + py) * r - py };
      setZoom(z);
    },
    [zoom],
  );

  // Pinch: native touch listeners (non-passive) so two-finger moves never scroll or zoom the page.
  useEffect(() => {
    const el = scroller.current;
    const inner = content.current;
    if (!el || !inner) return undefined;
    let start: { dist: number; cx: number; cy: number; sl: number; st: number } | null = null;
    let ratio = 1;
    const dist = (t: TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const onStart = (e: TouchEvent) => {
      if (e.touches.length !== 2) return;
      const rect = el.getBoundingClientRect();
      start = {
        dist: dist(e.touches),
        cx: (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left,
        cy: (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top,
        sl: el.scrollLeft,
        st: el.scrollTop,
      };
      ratio = 1;
    };
    const onMove = (e: TouchEvent) => {
      if (!start || e.touches.length !== 2) return;
      e.preventDefault();
      ratio = Math.min(MAX_ZOOM / zoom, Math.max(MIN_ZOOM / zoom, dist(e.touches) / start.dist));
      inner.style.transformOrigin = `${start.sl + start.cx}px ${start.st + start.cy}px`;
      inner.style.transform = `scale(${ratio})`;
    };
    const onEnd = (e: TouchEvent) => {
      if (!start || e.touches.length >= 2) return;
      inner.style.transform = '';
      const s = start;
      start = null;
      if (Math.abs(ratio - 1) > 0.02) {
        const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * ratio));
        const r = z / zoom;
        pendingScroll.current = { left: (s.sl + s.cx) * r - s.cx, top: (s.st + s.cy) * r - s.cy };
        setZoom(z);
      }
    };
    el.addEventListener('touchstart', onStart, { passive: true });
    el.addEventListener('touchmove', onMove, { passive: false });
    el.addEventListener('touchend', onEnd);
    el.addEventListener('touchcancel', onEnd);
    return () => {
      el.removeEventListener('touchstart', onStart);
      el.removeEventListener('touchmove', onMove);
      el.removeEventListener('touchend', onEnd);
      el.removeEventListener('touchcancel', onEnd);
    };
  }, [zoom, doc, sizes.length > 0]);

  // Double-tap: toggle 2x around the tap, back to fit width.
  const lastTap = useRef(0);
  const onClick = (e: ReactMouseEvent) => {
    const now = performance.now();
    if (now - lastTap.current < 300) {
      const rect = scroller.current!.getBoundingClientRect();
      zoomTo(zoom > 1.05 ? 1 : 2, e.clientX - rect.left, e.clientY - rect.top);
      lastTap.current = 0;
    } else {
      lastTap.current = now;
    }
  };

  const scrollToPage = useCallback(
    (page: number, offsetY = 0) => {
      const el = scroller.current;
      if (!el || !layout.tops.length) return;
      el.scrollTo({ top: Math.max(0, layout.tops[page] + offsetY - el.clientHeight * 0.3), behavior: 'smooth' });
    },
    [layout],
  );

  const runSearch = async () => {
    if (!doc) return;
    const q = query.trim();
    if (!q) {
      setMatches(null);
      return;
    }
    const n = Math.min(doc.numPages, MAX_PAGES);
    const all = new Map(texts);
    for (let i = 0; i < n; i++) {
      if (all.has(i)) continue;
      setSearching(`Searching page ${i + 1} of ${n}`);
      try {
        const tc = await (await doc.getPage(i + 1)).getTextContent();
        all.set(i, tc.items.filter((it): it is TextItem => 'str' in it));
      } catch {
        all.set(i, []);
      }
    }
    setTexts(all);
    setSearching(null);
    const pages: PdfTextItem[][] = Array.from({ length: n }, (_, i) => all.get(i) ?? []);
    const found = findInPages(pages, q);
    setMatches(found);
    setMatchIndex(0);
    if (found.length) scrollToPage(found[0].page);
  };

  const step = (d: 1 | -1) => {
    if (!matches?.length) return;
    const next = (matchIndex + d + matches.length) % matches.length;
    setMatchIndex(next);
    const m = matches[next];
    const item = texts.get(m.page)?.[m.segments[0].item];
    const scale = layout.pages[m.page]?.scale ?? 1;
    // PDF y grows upwards: distance from the page top is (page height - y) at this scale.
    const y = item ? (sizes[m.page].h - item.transform[5]) * scale : 0;
    scrollToPage(m.page, y);
  };

  const matchesByPage = useMemo(() => {
    const out = new Map<number, PdfMatch[]>();
    for (const m of matches ?? []) {
      const list = out.get(m.page);
      if (list) list.push(m);
      else out.set(m.page, [m]);
    }
    return out;
  }, [matches]);

  const submitSearch = (e: FormEvent) => {
    e.preventDefault();
    if (matches && matches.length && query.trim()) step(1);
    else void runSearch();
  };

  if (loadError) return <PreviewError src={src} error={loadError} />;
  if (!data) return <Loading label="Downloading the PDF" />;
  if (openError?.reason === 'password' || openError?.reason === 'wrong-password') {
    return opening ? <Loading label="Unlocking" /> : <PasswordPrompt src={src} wrong={openError.reason === 'wrong-password'} onSubmit={(pw) => void open(pw)} />;
  }
  if (openError) {
    return (
      <PreviewMessage src={src} icon="file" title={openError.reason === 'invalid' ? 'This PDF is damaged' : "Couldn't open this PDF"}>
        {openError.message}
      </PreviewMessage>
    );
  }
  if (!doc || !sizes.length) return <Loading label="Opening the PDF" />;

  const numPages = Math.min(doc.numPages, MAX_PAGES);
  const pageMatches = (i: number) => matchesByPage.get(i) ?? NO_MATCHES;
  const currentMatch = matches?.[matchIndex] ?? null;
  return (
    <div className="relative flex h-full flex-col bg-[#1b1d2b]">
      {/* Viewer, then (search open) the search bar at the bottom: both within thumb reach. */}
      <div className="relative min-h-0 flex-1">
        <div ref={scroller} onScroll={onScroll} onClick={onClick} className="h-full overflow-auto overscroll-contain" style={{ touchAction: 'pan-x pan-y' }}>
          <div ref={content} style={{ width: layout.contentWidth, height: layout.height, position: 'relative' }}>
            {layout.pages.map((p, i) => (
              <div key={i} className="absolute left-0 right-0" style={{ top: layout.tops[i], height: p.h }}>
                {i >= range[0] - 3 && i <= range[1] + 3 ? (
                  <PdfPage
                    doc={doc}
                    index={i}
                    width={p.w}
                    height={p.h}
                    scale={p.scale}
                    active={i >= range[0] && i <= range[1]}
                    textItems={texts.get(i)}
                    matches={pageMatches(i)}
                    currentMatch={currentMatch}
                  />
                ) : (
                  <div className="mx-auto bg-white/90" style={{ width: p.w, height: p.h }} />
                )}
              </div>
            ))}
          </div>
        </div>
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center px-3">
          <div className="pointer-events-auto flex items-center gap-0.5 rounded-full bg-ink-900/95 p-1 shadow-lg ring-1 ring-inset ring-ink-700 backdrop-blur">
            <span className="min-w-[64px] px-2 text-center text-[13px] font-semibold tabular-nums text-ink-100" aria-live="polite">
              {current + 1} / {numPages}
            </span>
            <IconButton icon="search" label="Search in the PDF" onClick={() => setSearchOpen((v) => !v)} className="h-10 w-10" />
            <button type="button" aria-label="Zoom out" onClick={() => zoomTo(zoom / 1.25)} className="press grid h-10 w-10 place-items-center rounded-full text-[20px] text-ink-200">
              −
            </button>
            <button
              type="button"
              onClick={() => zoomTo(1)}
              aria-label="Fit to width"
              className={cx('press min-h-[40px] rounded-full px-2 text-[12px] tabular-nums', Math.abs(zoom - 1) < 0.01 ? 'text-ink-400' : 'text-signal')}
            >
              {Math.round(zoom * 100)}%
            </button>
            <button type="button" aria-label="Zoom in" onClick={() => zoomTo(zoom * 1.25)} className="press grid h-10 w-10 place-items-center rounded-full text-[20px] text-ink-200">
              +
            </button>
            <IconButton icon="external" label="Open externally / Download" onClick={() => void src.download()} className="h-10 w-10" />
          </div>
        </div>
      </div>
      {searching && <p className="shrink-0 bg-ink-900 px-4 py-1 text-[12px] text-ink-400">{searching}</p>}
      {searchOpen && (
        <form onSubmit={submitSearch} className="flex shrink-0 items-center gap-1 border-t border-ink-800 bg-ink-900 px-2 py-1.5">
          <input
            type="search"
            enterKeyHint="search"
            autoFocus
            aria-label="Search in the PDF"
            placeholder="Find in document"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setMatches(null);
            }}
            className="min-h-[40px] min-w-0 flex-1 rounded-full bg-ink-850 px-4 text-[15px] text-ink-100 outline-none ring-1 ring-inset ring-ink-700 focus:ring-signal"
          />
          <span className="w-[64px] shrink-0 text-center text-[12px] text-ink-400" aria-live="polite">
            {searching ? <Spinner /> : matches ? (matches.length ? `${matchIndex + 1} / ${matches.length}` : 'None') : ''}
          </span>
          <IconButton icon="up" label="Previous match" type="button" onClick={() => step(-1)} disabled={!matches?.length} className="h-10 w-10" />
          <IconButton icon="down" label="Next match" type="submit" disabled={!!searching} className="h-10 w-10" />
          <IconButton
            icon="x"
            label="Close search"
            type="button"
            onClick={() => {
              setSearchOpen(false);
              setMatches(null);
            }}
            className="h-10 w-10"
          />
        </form>
      )}
    </div>
  );
}
