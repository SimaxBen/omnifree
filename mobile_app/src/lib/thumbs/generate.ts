// Client-side thumbnails for the Drive list/grid (cached by lib/thumbs/cache.ts):
//  * images: decoded with createImageBitmap, shortest side ~256 px, WebP (JPEG fallback);
//  * videos: a frame near 1 s from a hidden <video crossOrigin="anonymous"> (the bucket's CORS rule
//    must allow this origin), the browser fetches only the ranges it needs;
//  * PDFs: page 1 with pdf.js at a small scale;
//  * Office/OpenDocument: the thumbnail the app embedded (docProps/thumbnail.jpeg,
//    Thumbnails/thumbnail.png) read with HTTP Range requests, else the title / first words.
// At most two run at once (queue.ts), only for items near the viewport (FileThumb).

import type { HubClient } from '../api';
import { extensionOf, fileKind } from '../files';
import { CorsBlockedError, fetchObjectBytes, fetchObjectRange, objectUrl } from '../preview/source';
import type { StoredObject } from '../types';
import type { ThumbRecord } from './cache';
import { openRemoteZip, type RangeFetcher } from './zipRange';

export const THUMB_SIZE = 256;
export const IMAGE_THUMB_MAX = 25 * 1024 * 1024;
export const PDF_THUMB_MAX = 30 * 1024 * 1024;
/** The <video> element reads ranges (headers + one frame), so large files are fine; bounded anyway. */
export const VIDEO_THUMB_MAX = 1024 * 1024 * 1024;
/** Office files when the server ignores Range: read whole only up to this. */
const OFFICE_WHOLE_MAX = 12 * 1024 * 1024;

export type ThumbSource = 'image' | 'video' | 'pdf' | 'office';

const OFFICE = new Set(['docx', 'docm', 'xlsx', 'xlsm', 'pptx', 'pptm', 'odt', 'ods', 'odp']);
const NO_DECODE = new Set(['svg', 'heic', 'heif', 'ico']);

/** Which generator applies, or null (the type tile is enough). */
export function thumbSourceOf(obj: Pick<StoredObject, 'key' | 'content_type' | 'size'>): ThumbSource | null {
  const ext = extensionOf(obj.key);
  if (OFFICE.has(ext)) return 'office';
  const kind = fileKind(obj.key, obj.content_type);
  if (kind === 'image') return NO_DECODE.has(ext) || obj.size > IMAGE_THUMB_MAX ? null : 'image';
  if (kind === 'video') return obj.size > VIDEO_THUMB_MAX ? null : 'video';
  if (kind === 'pdf') return obj.size > PDF_THUMB_MAX ? null : 'pdf';
  return null;
}

async function canvasToRecord(canvas: HTMLCanvasElement | OffscreenCanvas): Promise<ThumbRecord> {
  let blob: Blob | null;
  if ('convertToBlob' in canvas) {
    blob = await canvas.convertToBlob({ type: 'image/webp', quality: 0.8 });
  } else {
    blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/webp', 0.8));
  }
  if (!blob) throw new Error('Canvas export failed');
  return { kind: 'image', data: await blob.arrayBuffer(), mime: blob.type || 'image/webp' };
}

function makeCanvas(w: number, h: number): HTMLCanvasElement | OffscreenCanvas {
  if (typeof OffscreenCanvas !== 'undefined') return new OffscreenCanvas(w, h);
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  return c;
}

/** Shortest side THUMB_SIZE (never upscaled), longest side at most twice that. */
export function thumbDims(w: number, h: number, size = THUMB_SIZE): { w: number; h: number } {
  const s = Math.min(1, size / Math.min(w, h), (size * 2) / Math.max(w, h));
  return { w: Math.max(1, Math.round(w * s)), h: Math.max(1, Math.round(h * s)) };
}

function drawScaled(source: CanvasImageSource, w: number, h: number): HTMLCanvasElement | OffscreenCanvas {
  const d = thumbDims(w, h);
  const canvas = makeCanvas(d.w, d.h);
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D | null;
  if (!ctx) throw new Error('No 2D canvas');
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(source, 0, 0, d.w, d.h);
  return canvas;
}

async function imageThumb(client: HubClient, obj: StoredObject, signal: AbortSignal): Promise<ThumbRecord> {
  const bytes = await fetchObjectBytes(client, obj, IMAGE_THUMB_MAX, signal);
  const bitmap = await createImageBitmap(new Blob([bytes], { type: obj.content_type || 'image/*' }));
  try {
    return await canvasToRecord(drawScaled(bitmap, bitmap.width, bitmap.height));
  } finally {
    bitmap.close();
  }
}

function videoThumb(client: HubClient, obj: StoredObject, signal: AbortSignal): Promise<ThumbRecord> {
  return objectUrl(client, obj, signal).then(
    (url) =>
      new Promise<ThumbRecord>((resolve, reject) => {
        const v = document.createElement('video');
        let settled = false;
        const finish = (err: unknown, rec?: ThumbRecord) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timer);
          signal.removeEventListener('abort', onAbort);
          v.removeAttribute('src');
          v.load(); // releases the connection and decoder
          if (rec) resolve(rec);
          else reject(err);
        };
        const onAbort = () => finish(new DOMException('aborted', 'AbortError'));
        const timer = window.setTimeout(() => finish(new Error('Video frame timed out')), 15_000);
        signal.addEventListener('abort', onAbort);
        // Without CORS the frame would taint the canvas; with it, a missing rule fails the load.
        if (!url.startsWith('blob:')) v.crossOrigin = 'anonymous';
        v.muted = true;
        v.playsInline = true;
        v.preload = 'metadata';
        v.onloadedmetadata = () => {
          const d = Number.isFinite(v.duration) ? v.duration : 0;
          v.currentTime = d > 2 ? 1 : d / 2;
        };
        v.onseeked = () => {
          try {
            if (!v.videoWidth) throw new Error('No video frame');
            canvasToRecord(drawScaled(v, v.videoWidth, v.videoHeight)).then(
              (rec) => finish(null, rec),
              (err) => finish(err),
            );
          } catch (err) {
            finish(err);
          }
        };
        v.onerror = () => finish(url.startsWith('blob:') ? new Error('Unplayable video') : new CorsBlockedError());
        v.src = url;
      }),
  );
}

async function pdfThumb(client: HubClient, obj: StoredObject, signal: AbortSignal): Promise<ThumbRecord> {
  const bytes = await fetchObjectBytes(client, obj, PDF_THUMB_MAX, signal);
  const { openPdf } = await import('../preview/pdfjs');
  const doc = await openPdf(bytes);
  try {
    const page = await doc.getPage(1);
    const base = page.getViewport({ scale: 1 });
    const d = thumbDims(base.width, base.height);
    const viewport = page.getViewport({ scale: d.w / base.width });
    const canvas = document.createElement('canvas');
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const ctx = canvas.getContext('2d');
    if (ctx) {
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    await page.render({ canvas, viewport, background: '#ffffff' }).promise;
    const rec = await canvasToRecord(canvas);
    canvas.width = 0;
    return rec;
  } finally {
    void doc.loadingTask.destroy();
  }
}

const EMBEDDED = ['docProps/thumbnail.jpeg', 'docProps/thumbnail.jpg', 'docProps/thumbnail.png', 'Thumbnails/thumbnail.png'];

function xmlText(xml: string, tag: RegExp): string {
  const parts: string[] = [];
  for (const m of xml.matchAll(tag)) parts.push(m[1]);
  return parts
    .join(' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, n: string) => String.fromCodePoint(Number(n)))
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

async function officeThumb(client: HubClient, obj: StoredObject, signal: AbortSignal): Promise<ThumbRecord> {
  let whole: ArrayBuffer | null = null;
  const fetchRange: RangeFetcher = async (start, end) => {
    if (whole) return whole.slice(start, end + 1);
    const r = await fetchObjectRange(client, obj, start, end, OFFICE_WHOLE_MAX, signal);
    if (r.whole) whole = r.whole;
    return r.data;
  };
  const zip = await openRemoteZip(fetchRange, obj.size);
  for (const name of EMBEDDED) {
    const bytes = await zip.read(name, 3 * 1024 * 1024);
    if (bytes?.length) {
      const png = bytes[0] === 0x89 && bytes[1] === 0x50;
      const jpeg = bytes[0] === 0xff && bytes[1] === 0xd8;
      if (png || jpeg) return { kind: 'image', data: bytes.slice().buffer as ArrayBuffer, mime: png ? 'image/png' : 'image/jpeg' };
    }
  }
  // No picture: the title, else the first words, for a "paper" tile.
  const dec = new TextDecoder();
  const read = async (name: string) => {
    const b = await zip.read(name, 48 * 1024, { maxCompressed: 64 * 1024 });
    return b ? dec.decode(b) : '';
  };
  const ext = extensionOf(obj.key);
  let text = xmlText(await read('docProps/core.xml'), /<dc:title>([^<]*)<\/dc:title>/g);
  if (!text && ext.startsWith('doc')) text = xmlText(await read('word/document.xml'), /<w:t(?:\s[^>]*)?>([^<]*)<\/w:t>/g);
  if (!text && ext.startsWith('ppt')) text = xmlText(await read('ppt/slides/slide1.xml'), /<a:t>([^<]*)<\/a:t>/g);
  if (!text && ext === 'odt') text = xmlText(await read('content.xml'), /<text:[ph][^>]*>([^<]*)</g);
  return text ? { kind: 'text', text: text.slice(0, 160) } : { kind: 'none' };
}

export async function generateThumb(client: HubClient, obj: StoredObject, signal: AbortSignal): Promise<ThumbRecord> {
  switch (thumbSourceOf(obj)) {
    case 'image':
      return imageThumb(client, obj, signal);
    case 'video':
      return videoThumb(client, obj, signal);
    case 'pdf':
      return pdfThumb(client, obj, signal);
    case 'office':
      return officeThumb(client, obj, signal);
    default:
      return { kind: 'none' };
  }
}
