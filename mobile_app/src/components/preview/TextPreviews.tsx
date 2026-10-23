// Text, code and Markdown previews. Code is highlighted by a lazily loaded highlight.js; plain text
// and anything too big to highlight render as React text (never as HTML).

import { useEffect, useMemo, useState } from 'react';
import { codeLanguage, extensionOf } from '../../lib/files';
import { copyText } from '../../lib/dom';
import { useHub } from '../../lib/hub';
import type { PreviewSource } from '../../lib/preview/source';
import { splitHighlighted } from '../../lib/preview/lines';
import { Markdown } from '../Markdown';
import { Segmented, cx } from '../ui';
import { Icon } from '../icons';
import { Loading, PreviewError, decodeText, useSourceBytes } from './common';

export const TEXT_MAX_BYTES = 2 * 1024 * 1024;
const HIGHLIGHT_MAX_CHARS = 300_000;
const MAX_LINES = 20_000;
const BLOCK = 200;

function useText(src: PreviewSource): { text: string | null; error: unknown } {
  const { data, error } = useSourceBytes(src, TEXT_MAX_BYTES);
  const text = useMemo(() => {
    if (!data) return null;
    let t = decodeText(data);
    if (extensionOf(src.path) === 'json') {
      try {
        t = JSON.stringify(JSON.parse(t), null, 2);
      } catch {
        // Show it as it is.
      }
    }
    return t;
  }, [data, src.path]);
  return { text, error };
}

export function CodeView({ text, language }: { text: string; language: string }) {
  const { notify } = useHub();
  const [wrap, setWrap] = useState(false);
  const [html, setHtml] = useState<string[] | null>(null);
  const lines = useMemo(() => text.replace(/\r\n?/g, '\n').split('\n'), [text]);
  const shown = Math.min(lines.length, MAX_LINES);
  useEffect(() => {
    setHtml(null);
    if (!language || text.length > HIGHLIGHT_MAX_CHARS) return undefined;
    let alive = true;
    import('../../lib/preview/highlight').then(
      ({ highlightCode }) => {
        const out = highlightCode(lines.slice(0, shown).join('\n'), language);
        if (alive && out !== null) setHtml(splitHighlighted(out));
      },
      () => undefined, // Highlighting is a nicety: plain text stays.
    );
    return () => {
      alive = false;
    };
  }, [lines, shown, language, text.length]);

  const blocks: number[] = [];
  for (let i = 0; i < shown; i += BLOCK) blocks.push(i);
  const digits = String(shown).length;
  return (
    <div className="flex h-full flex-col bg-ink-950">
      <div className="flex shrink-0 items-center gap-2 border-b border-ink-800 px-3 py-1.5 text-[12px] text-ink-400">
        <span className="min-w-0 flex-1 truncate">
          {lines.length.toLocaleString()} lines{language ? `, ${language}` : ''}
          {lines.length > shown ? `, first ${shown.toLocaleString()} shown` : ''}
        </span>
        <button type="button" onClick={() => setWrap((w) => !w)} aria-pressed={wrap} className={cx('press min-h-[36px] rounded-full px-3', wrap ? 'bg-signal/15 text-signal' : 'bg-ink-850 text-ink-200')}>
          Wrap
        </button>
        <button
          type="button"
          onClick={async () => {
            const ok = await copyText(text);
            notify(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'error');
          }}
          className="press flex min-h-[36px] items-center gap-1.5 rounded-full bg-ink-850 px-3 text-ink-200"
        >
          <Icon name="copy" size={14} />
          Copy
        </button>
      </div>
      <div className="code-view min-h-0 flex-1 overflow-auto overscroll-contain">
        <pre className={cx('min-w-full py-3 font-mono text-[12.5px] leading-[1.6] text-ink-200', wrap ? 'whitespace-pre-wrap break-words' : 'w-max whitespace-pre')}>
          {blocks.map((start) => (
            <div key={start} className="code-block">
              {lines.slice(start, Math.min(start + BLOCK, shown)).map((line, j) => {
                const i = start + j;
                return (
                  <div key={i} className="flex">
                    <span className="sticky left-0 shrink-0 select-none bg-ink-950 pl-3 pr-3 text-right text-ink-600" style={{ width: `calc(${digits}ch + 1.5rem)` }}>
                      {i + 1}
                    </span>
                    {html ? (
                      // Escaped highlighter output (spans only), see lib/preview/highlight.ts.
                      <span className="min-w-0 flex-1 pr-4" dangerouslySetInnerHTML={{ __html: html[i] || ' ' }} />
                    ) : (
                      <span className="min-w-0 flex-1 pr-4">{line || ' '}</span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </pre>
      </div>
    </div>
  );
}

export function CodePreview({ src, plain }: { src: PreviewSource; plain?: boolean }) {
  const { text, error } = useText(src);
  if (error) return <PreviewError src={src} error={error} />;
  if (text === null) return <Loading />;
  return <CodeView text={text} language={plain ? '' : codeLanguage(src.path)} />;
}

export function MarkdownPreview({ src }: { src: PreviewSource }) {
  const { text, error } = useText(src);
  const [mode, setMode] = useState<'rendered' | 'raw'>('rendered');
  if (error) return <PreviewError src={src} error={error} />;
  if (text === null) return <Loading />;
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 justify-center border-b border-ink-800 px-3 py-2">
        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { value: 'rendered', label: 'Rendered' },
            { value: 'raw', label: 'Raw' },
          ]}
        />
      </div>
      <div className="min-h-0 flex-1">
        {mode === 'raw' ? (
          <CodeView text={text} language="markdown" />
        ) : (
          <div className="h-full overflow-y-auto overscroll-contain bg-ink-900 px-5 py-6">
            <Markdown text={text} className="mx-auto max-w-prose" />
          </div>
        )}
      </div>
    </div>
  );
}
