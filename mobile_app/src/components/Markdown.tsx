import { Fragment, type ReactNode } from 'react';
import { parseBlocks, parseInline, type Inline } from '../lib/markdown';
import { cx } from './ui';

function renderInline(nodes: Inline[], onCite?: (n: number) => void, activeCite?: number | null): ReactNode {
  return nodes.map((n, i) => {
    switch (n.t) {
      case 'text':
        return <Fragment key={i}>{n.v}</Fragment>;
      case 'strong':
        return (
          <strong key={i} className="font-semibold text-ink-100">
            {renderInline(n.c, onCite, activeCite)}
          </strong>
        );
      case 'em':
        return <em key={i}>{renderInline(n.c, onCite, activeCite)}</em>;
      case 'code':
        return (
          <code key={i} className="rounded-md bg-ink-800 px-1.5 py-0.5 font-mono text-[13px] text-ink-100">
            {n.v}
          </code>
        );
      case 'link':
        return (
          <a key={i} href={n.href} target="_blank" rel="noopener noreferrer" className="text-signal underline decoration-signal/40 underline-offset-4">
            {renderInline(n.c, onCite, activeCite)}
          </a>
        );
      case 'cite':
        return onCite ? (
          <button
            key={i}
            type="button"
            onClick={() => onCite(n.n)}
            aria-label={`Source ${n.n}`}
            aria-pressed={activeCite === n.n}
            className={cx(
              'press mx-0.5 inline-grid h-6 min-w-[24px] place-items-center rounded-full px-1.5 align-[1px] text-[12px] font-semibold',
              activeCite === n.n ? 'bg-signal text-ink-950' : 'bg-signal/15 text-signal',
            )}
          >
            {n.n}
          </button>
        ) : (
          <sup key={i}>[{n.n}]</sup>
        );
      default:
        return null;
    }
  });
}

/** Renders the Markdown subset from lib/markdown as React elements (never raw HTML). */
export function Markdown({
  text,
  onCite,
  activeCite,
  onToggle,
  className,
}: {
  text: string;
  onCite?: (n: number) => void;
  activeCite?: number | null;
  onToggle?: (checkboxIndex: number) => void;
  className?: string;
}) {
  let box = -1;
  const inline = (t: string) => renderInline(parseInline(t), onCite, activeCite);
  return (
    <div className={cx('space-y-3 text-[15px] leading-relaxed text-ink-200', className)}>
      {parseBlocks(text).map((b, i) => {
        switch (b.t) {
          case 'heading': {
            const size = b.level === 1 ? 'text-[22px]' : b.level === 2 ? 'text-[19px]' : 'text-[17px]';
            return (
              <p key={i} className={cx('font-display font-semibold text-ink-100', size)}>
                {inline(b.text)}
              </p>
            );
          }
          case 'para':
            return (
              <p key={i} className="whitespace-pre-wrap break-words">
                {inline(b.text)}
              </p>
            );
          case 'quote':
            return (
              <blockquote key={i} className="border-l-2 border-signal/50 pl-3 text-ink-300">
                {inline(b.text)}
              </blockquote>
            );
          case 'code':
            return (
              <pre key={i} className="overflow-x-auto rounded-row bg-ink-950 p-3 font-mono text-[13px] text-ink-200">
                {b.text}
              </pre>
            );
          case 'hr':
            return <hr key={i} className="border-ink-700" />;
          case 'list': {
            const Tag = b.ordered ? 'ol' : 'ul';
            const isTasks = b.items.some((x) => x.checked !== null);
            return (
              <Tag key={i} className={cx('space-y-1.5', b.ordered ? 'list-decimal pl-6' : isTasks ? '' : 'list-disc pl-6')}>
                {b.items.map((item, j) => {
                  if (item.checked === null) return <li key={j}>{inline(item.text)}</li>;
                  box++;
                  const idx = box;
                  return (
                    <li key={j} className="flex items-start gap-2.5">
                      <button
                        type="button"
                        role="checkbox"
                        aria-checked={item.checked}
                        aria-label={item.text}
                        disabled={!onToggle}
                        onClick={() => onToggle?.(idx)}
                        className={cx(
                          'press mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-md ring-1 ring-inset',
                          item.checked ? 'bg-signal text-ink-950 ring-signal' : 'ring-ink-500',
                        )}
                      >
                        {item.checked && (
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                            <path d="M5 12.5l4.5 4.5L19 7.5" />
                          </svg>
                        )}
                      </button>
                      <span className={cx(item.checked && 'text-ink-500 line-through')}>{inline(item.text)}</span>
                    </li>
                  );
                })}
              </Tag>
            );
          }
          default:
            return null;
        }
      })}
    </div>
  );
}
