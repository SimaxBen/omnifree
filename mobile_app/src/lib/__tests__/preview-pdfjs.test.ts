import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls: Record<string, unknown>[] = [];
let workersMade = 0;

vi.mock('pdfjs-dist', () => {
  class PasswordException extends Error {
    constructor(
      msg: string,
      public code: number,
    ) {
      super(msg);
    }
  }
  class InvalidPDFException extends Error {}
  class PDFWorker {
    destroyed = false;
    constructor() {
      workersMade++;
    }
    destroy() {
      this.destroyed = true;
    }
  }
  return {
    version: '9.9.9',
    GlobalWorkerOptions: {},
    PasswordException,
    InvalidPDFException,
    PDFWorker,
    Util: {},
    getDocument: (params: Record<string, unknown>) => {
      calls.push(params);
      const pw = params.password as string | undefined;
      return {
        promise: pw === 'ok' || pw === undefined ? Promise.resolve({ numPages: 1 }) : Promise.reject(new PasswordException('bad', 2)),
        destroy: () => Promise.resolve(),
      };
    },
  };
});

const { openPdf, PDFJS_BASE, PdfError, sharedWorker } = await import('../preview/pdfjs');

describe('pdf.js loader', () => {
  beforeEach(() => {
    calls.length = 0;
  });

  it('serves its data files from a versioned path (CacheFirst-safe across upgrades)', async () => {
    expect(PDFJS_BASE).toMatch(/\/pdfjs\/9\.9\.9\/$/);
    await openPdf(new ArrayBuffer(8));
    const p = calls[0];
    expect(p.wasmUrl).toBe(`${PDFJS_BASE}wasm/`);
    expect(p.cMapUrl).toBe(`${PDFJS_BASE}cmaps/`);
    expect(p.standardFontDataUrl).toBe(`${PDFJS_BASE}standard_fonts/`);
    expect(p.enableXfa).toBe(false);
  });

  it('reuses one worker for every document and hands the bytes over without a copy', async () => {
    const buf = new ArrayBuffer(16);
    await openPdf(buf);
    await openPdf(new ArrayBuffer(4));
    expect(workersMade).toBe(1);
    expect(calls[0].worker).toBe(calls[1].worker);
    expect(calls[0].worker).toBe(sharedWorker());
    expect((calls[0].data as Uint8Array).buffer).toBe(buf);
  });

  it('makes a new worker once the shared one is destroyed', () => {
    const w = sharedWorker();
    w.destroy();
    expect(sharedWorker()).not.toBe(w);
  });

  it('maps a wrong password to a PdfError', async () => {
    await expect(openPdf(new ArrayBuffer(4), 'nope')).rejects.toBeInstanceOf(PdfError);
    await expect(openPdf(new ArrayBuffer(4), 'nope')).rejects.toMatchObject({ reason: 'wrong-password' });
  });
});
