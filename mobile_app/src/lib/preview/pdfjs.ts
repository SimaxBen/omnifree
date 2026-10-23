// pdf.js, loaded on demand. The worker, wasm decoders, standard fonts and CMaps are served from
// this app's own build (vite.config.ts copies them to /pdfjs/), never from a CDN.

import { GlobalWorkerOptions, InvalidPDFException, PasswordException, getDocument, type PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc = workerUrl;

const BASE = `${import.meta.env.BASE_URL}pdfjs/`;

export type PdfOpenError = 'password' | 'wrong-password' | 'invalid' | 'other';

export class PdfError extends Error {
  constructor(
    public reason: PdfOpenError,
    message: string,
  ) {
    super(message);
  }
}

/** Opens a PDF from bytes (a copy is handed to the worker, so `data` stays usable for a retry). */
export async function openPdf(data: ArrayBuffer, password?: string): Promise<PDFDocumentProxy> {
  const task = getDocument({
    data: new Uint8Array(data.slice(0)),
    password,
    enableXfa: false,
    wasmUrl: `${BASE}wasm/`,
    standardFontDataUrl: `${BASE}standard_fonts/`,
    cMapUrl: `${BASE}cmaps/`,
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
