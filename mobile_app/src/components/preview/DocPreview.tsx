// Word (.docx via mammoth) and OpenDocument text (.odt) as reading-style HTML on a light page.
// Both converters' output is untrusted and goes through DOMPurify (lib/preview/sanitize.ts).

import JSZip from 'jszip';
import mammoth from 'mammoth';
import { useEffect, useState } from 'react';
import { odtToHtml } from '../../lib/preview/odt';
import { sanitizeDocHtml } from '../../lib/preview/sanitize';
import type { PreviewSource } from '../../lib/preview/source';
import { Loading, PreviewError, useSourceBytes } from './common';

export const DOC_MAX_BYTES = 30 * 1024 * 1024;

async function convert(data: ArrayBuffer, format: 'docx' | 'odt'): Promise<{ html: string; warnings: number }> {
  if (format === 'odt') {
    const zip = await JSZip.loadAsync(data);
    return { html: await odtToHtml(zip), warnings: 0 };
  }
  const res = await mammoth.convertToHtml({ arrayBuffer: data }, { ignoreEmptyParagraphs: true, idPrefix: 'doc-' });
  return { html: res.value, warnings: res.messages.filter((m) => m.type === 'warning').length };
}

export default function DocPreview({ src, format }: { src: PreviewSource; format: 'docx' | 'odt' }) {
  const { data, error: loadError } = useSourceBytes(src, DOC_MAX_BYTES);
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!data) return undefined;
    let alive = true;
    setHtml(null);
    setError(null);
    convert(data, format).then(
      (r) => alive && setHtml(sanitizeDocHtml(r.html)),
      (err) =>
        alive &&
        setError(
          /zip|central directory|end of data/i.test(String(err))
            ? `This doesn't look like a valid .${format} file (it may be damaged, password-protected or an older format).`
            : err,
        ),
    );
    return () => {
      alive = false;
    };
  }, [data, format]);

  if (loadError || error) return <PreviewError src={src} error={loadError || error} />;
  if (html === null) return <Loading label={data ? 'Converting the document' : 'Downloading the document'} />;
  return (
    <div className="h-full overflow-y-auto overscroll-contain bg-[#1b1d2b] px-2 py-3">
      {html.trim() ? (
        // Sanitised with DOMPurify (no script, handlers, frames, styles or external images).
        <article className="doc-paper mx-auto max-w-[720px] rounded-[6px] bg-[#FBFAF6] px-5 py-6 text-[15px] leading-relaxed text-[#1d1d24] shadow-[0_2px_14px_rgba(0,0,0,0.45)]" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <p className="p-6 text-center text-[14px] text-ink-400">This document has no text to show.</p>
      )}
    </div>
  );
}
