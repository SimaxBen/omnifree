import { Fragment, useState, type ReactNode } from 'react';
import { parseBlocks, parseInline, type Align, type Block, type Inline } from '../lib/markdown';
import { cx } from './ui';

interface Ctx {
  onCite?: (n: number) => void;
  activeCite?: number | null;
  onToggle?: (checkboxIndex: number) => void;
  /** [[Wiki link]] tapped: the linked note title. Without it, links render as plain styled text. */
  onWikiLink?: (title: string) => void;
  /** #tag tapped (lowercase, without the #). */
  onTag?: (tag: string) => void;
  /** Running checkbox index, in document order (matches toggleCheckbox). */
  box: { n: number };
  /** Inside a link: nested links and tags render as text (no button inside an anchor). */
  inLink?: boolean;
}

function renderInline(nodes: Inline[], ctx: Ctx): ReactNode {
  return nodes.map((n, i) => {
    switch (n.t) {
      case 'text':
        return <Fragment key={i}>{n.v}</Fragment>;
      case 'br':
        return <br key={i} />;
      case 'strong':
        return (
          <strong key={i} className="font-semibold text-ink-100">
            {renderInline(n.c, ctx)}
          </strong>
        );
      case 'em':
        return <em key={i}>{renderInline(n.c, ctx)}</em>;
      case 'strike':
        return (
          <del key={i} className="text-ink-400 decoration-ink-400/70">
            {renderInline(n.c, ctx)}
          </del>
        );
      case 'code':
        return (
          <code key={i} className="break-words rounded-md bg-ink-800 px-1.5 py-0.5 font-mono text-[0.87em] text-ink-100">
            {n.v}
          </code>
        );
      case 'link':
        return (
          <a key={i} href={n.href} target="_blank" rel="noopener noreferrer" className="break-words text-signal underline decoration-signal/40 underline-offset-4">
            {renderInline(n.c, { ...ctx, inLink: true })}
          </a>
        );
      case 'wikilink':
        return ctx.onWikiLink && !ctx.inLink ? (
          <button
            key={i}
            type="button"
            onClick={() => ctx.onWikiLink?.(n.title)}
            title={n.label !== n.title ? n.title : undefined}
            className="press inline break-words rounded-md bg-signal/10 px-1 text-left font-medium text-signal underline decoration-signal/40 decoration-dotted underline-offset-4"
          >
            {n.label}
          </button>
        ) : (
          <span key={i} className="break-words font-medium text-signal">
            {n.label}
          </span>
        );
      case 'tag':
        return ctx.onTag && !ctx.inLink ? (
          <button key={i} type="button" onClick={() => ctx.onTag?.(n.tag)} aria-label={`Notes tagged ${n.tag}`} className="press inline text-signal/90 hover:text-signal">
            #{n.tag}
          </button>
        ) : (
          <span key={i} className="text-signal/90">
            #{n.tag}
          </span>
        );
      case 'cite':
        return ctx.onCite ? (
          <button
            key={i}
            type="button"
            onClick={() => ctx.onCite?.(n.n)}
            aria-label={`Source ${n.n}`}
            aria-pressed={ctx.activeCite === n.n}
            className={cx(
              'press mx-0.5 inline-grid h-6 min-w-[24px] place-items-center rounded-full px-1.5 align-[1px] text-[12px] font-semibold',
              ctx.activeCite === n.n ? 'bg-signal text-ink-950' : 'bg-signal/15 text-signal',
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

/** A fenced code block with its language and a copy button. */
function CodeBlock({ lang, text }: { lang: string; text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="overflow-hidden rounded-row bg-ink-950 ring-1 ring-inset ring-ink-800">
      <div className="flex min-h-[36px] items-center justify-between gap-2 border-b border-ink-800 pl-3 pr-1">
        <span className="truncate font-mono text-[11px] uppercase tracking-wide text-ink-500">{lang || 'code'}</span>
        <button
          type="button"
          onClick={() => void copy()}
          aria-label={copied ? 'Copied' : 'Copy code'}
          className={cx('press min-h-[32px] rounded-full px-3 text-[12px] font-semibold', copied ? 'text-mint-400' : 'text-ink-300')}
        >
          {copied ? 'Copied ✓' : 'Copy'}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[13px] leading-relaxed text-ink-200">
        <code>{text}</code>
      </pre>
    </div>
  );
}

const ALIGN: Record<Exclude<Align, null>, string> = { left: 'text-left', center: 'text-center', right: 'text-right' };

const HEADING_SIZE = { 1: 'text-[22px]', 2: 'text-[19px]', 3: 'text-[17px]', 4: 'text-[15px] uppercase tracking-wide text-ink-300' } as const;

function renderBlocks(blocks: Block[], ctx: Ctx, depth = 0): ReactNode {
  const inline = (t: string) => renderInline(parseInline(t), ctx);
  return blocks.map((b, i) => {
    switch (b.t) {
      case 'heading':
        return (
          <p key={i} role="heading" aria-level={b.level} className={cx('break-words font-display font-semibold text-ink-100', HEADING_SIZE[b.level], i > 0 && 'pt-1')}>
            {inline(b.text)}
          </p>
        );
      case 'para':
        return (
          <p key={i} className="break-words">
            {inline(b.text)}
          </p>
        );
      case 'quote':
        return (
          <blockquote key={i} className="space-y-2 border-l-2 border-signal/50 pl-3 text-ink-300">
            {renderBlocks(b.blocks, ctx, depth)}
          </blockquote>
        );
      case 'code':
        return <CodeBlock key={i} lang={b.lang} text={b.text} />;
      case 'hr':
        return <hr key={i} className="my-1 h-px border-0 bg-ink-700" />;
      case 'details':
        return (
          <details key={i} className="group rounded-row bg-ink-900 ring-1 ring-inset ring-ink-800">
            <summary className="press flex min-h-[44px] cursor-pointer list-none items-center gap-2 px-3 font-semibold text-ink-200 [&::-webkit-details-marker]:hidden">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="shrink-0 transition-transform duration-base group-open:rotate-90">
                <path d="M9 5l7 7-7 7" />
              </svg>
              <span className="min-w-0 break-words">{inline(b.summary)}</span>
            </summary>
            <div className="space-y-3 border-t border-ink-800 px-3 py-3">{renderBlocks(b.blocks, ctx, depth)}</div>
          </details>
        );
      case 'table':
        return (
          <div key={i} role="region" aria-label="Table" tabIndex={0} className="overflow-x-auto overscroll-x-contain rounded-row ring-1 ring-inset ring-ink-800">
            <table className="min-w-full border-collapse text-left text-[14px] leading-snug">
              <thead className="bg-ink-850">
                <tr>
                  {b.head.map((cell, c) => (
                    <th key={c} scope="col" className={cx('border-b border-ink-700 px-3 py-2 align-bottom font-semibold text-ink-100', b.align[c] && ALIGN[b.align[c]!])}>
                      <div className="min-w-[4.5rem] max-w-[16rem]">{inline(cell)}</div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row, r) => (
                  <tr key={r} className="border-t border-ink-800 first:border-t-0 even:bg-ink-900/70">
                    {row.map((cell, c) => (
                      <td key={c} className={cx('px-3 py-2 align-top', b.align[c] && ALIGN[b.align[c]!])}>
                        <div className="min-w-[4.5rem] max-w-[16rem] break-words">{inline(cell)}</div>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case 'list': {
        const Tag = b.ordered ? 'ol' : 'ul';
        const isTasks = b.items.some((x) => x.checked !== null);
        const bullet = depth % 2 === 0 ? 'list-disc' : 'list-[circle]';
        return (
          <Tag
            key={i}
            start={b.ordered && b.start !== 1 ? b.start : undefined}
            className={cx('space-y-1.5 marker:text-ink-400', b.ordered ? 'list-decimal pl-6' : isTasks ? '' : `${bullet} pl-5`)}
          >
            {b.items.map((item, j) => {
              const children = item.children.length > 0 && <div className="mt-1.5 space-y-2">{renderBlocks(item.children, ctx, depth + 1)}</div>;
              if (item.checked === null) {
                return (
                  <li key={j} className="break-words pl-0.5">
                    {inline(item.text)}
                    {children}
                  </li>
                );
              }
              ctx.box.n++;
              const idx = ctx.box.n;
              const { onToggle } = ctx;
              return (
                <li key={j}>
                  <div className="flex items-start gap-2.5">
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
                    <span className={cx('min-w-0 break-words', item.checked && 'text-ink-500 line-through')}>{inline(item.text)}</span>
                  </div>
                  {children && <div className="pl-[30px]">{children}</div>}
                </li>
              );
            })}
          </Tag>
        );
      }
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
  onWikiLink,
  onTag,
  className,
}: {
  text: string;
  onCite?: (n: number) => void;
  activeCite?: number | null;
  onToggle?: (checkboxIndex: number) => void;
  onWikiLink?: (title: string) => void;
  onTag?: (tag: string) => void;
  className?: string;
}) {
  const ctx: Ctx = { onCite, activeCite, onToggle, onWikiLink, onTag, box: { n: -1 } };
  return <div className={cx('min-w-0 space-y-3 text-[15px] leading-relaxed text-ink-200', className)}>{renderBlocks(parseBlocks(text), ctx)}</div>;
}
