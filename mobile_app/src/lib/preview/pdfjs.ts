// pdf.js, loaded on demand. The worker, wasm decoders, standard fonts and CMaps are served from
// this app's own build (vite.config.ts copies them to /pdfjs/<version>/), never from a CDN. The
// version is in the path because the service worker keeps those files CacheFirst: an upgraded
// pdf.js must never be handed the previous version's wasm.
//
// Security: pdf.js 6 has no eval path (the old `isEvalSupported` option is gone), no scripting is
// loaded (no pdf_viewer, the quickjs sandbox is not shipped), XFA is off, and no annotation layer
// is rendered, so PDF links are never clickable in the app.

import { GlobalWorkerOptions, InvalidPDFException, PDFWorker, PasswordException, getDocument, version, type PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc = workerUrl;

export const PDFJS_BASE = `${import.meta.env.BASE_URL}pdfjs/${version}/`;

export type PdfOpenError = 'password' | 'wrong-password' | 'invalid' | 'other';

export class PdfError extends Error {
  constructor(
    public reason: PdfOpenError,
    message: string,
  ) {
    super(message);
  }
}

let worker: PDFWorker | null = null;

/** One pdf.js worker for the session (viewer and thumbnails): no new worker per file. */
export function sharedWorker(): PDFWorker {
  if (!worker || worker.destroyed) worker = new PDFWorker();
  return worker;
}

/**
 * Opens a PDF from bytes. `data` is handed (transferred) to the worker, so it is unusable
 * afterwards: callers that may retry (wrong password) fetch the bytes again.
 */
export async function openPdf(data: ArrayBuffer, password?: string): Promise<PDFDocumentProxy> {
  const task = getDocument({
    data: new Uint8Array(data),
    password,
    worker: sharedWorker(),
    enableXfa: false,
    wasmUrl: `${PDFJS_BASE}wasm/`,
    standardFontDataUrl: `${PDFJS_BASE}standard_fonts/`,
    cMapUrl: `${PDFJS_BASE}cmaps/`,
    cMapPacked: true,
    stopAtErrors: false,
  });
  try {
    return await task.promise;
  } catch (err) {
    void task.destroy();
    if (err instanceof PasswordException) {
      // code 1: a password is needed; 2: the one given is wrong.
      throw new PdfError(err.code === 2 ? 'wrong-password' : 'password', err.code === 2 ? 'Wrong password' : 'This PDF is password-protected.');
    }
    if (err instanceof InvalidPDFException) throw new PdfError('invalid', 'This file is damaged or not a valid PDF.');
    throw new PdfError('other', err instanceof Error ? err.message : String(err));
  }
}

/** Canvas backing-store scale: devicePixelRatio, capped (memory), and a pixel budget per page. */
export function outputScale(cssWidth: number, cssHeight: number, maxDpr = 2, maxPixels = 8_000_000): number {
  const dpr = Math.min(typeof devicePixelRatio === 'number' ? devicePixelRatio : 1, maxDpr);
  const area = cssWidth * cssHeight * dpr * dpr;
  return area > maxPixels ? Math.sqrt(maxPixels / (cssWidth * cssHeight)) : dpr;
}

export { Util } from 'pdfjs-dist';
export type { PDFDocumentProxy, PDFPageProxy, PageViewport } from 'pdfjs-dist';
export type { TextItem } from 'pdfjs-dist/types/src/display/api';
