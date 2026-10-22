// A safe Markdown subset for notes, Ask-my-Drive answers and AI chat replies. It produces tokens that
// the <Markdown> component renders as React elements, so no HTML string is ever injected (raw HTML in the
// source stays visible text).
//
// Blocks: ATX headings (# to ####; deeper levels render as h4), paragraphs with line breaks, bullet lists
// (`-`, `*`, `+`) and ordered lists (`1.` / `1)`, the first number is kept) nested by indentation, task items
// (`- [ ]`), GFM tables (header row + `|---|` separator, column alignment), blockquotes (with blocks inside),
// fenced code (``` or ~~~, with a language) and horizontal rules (---, ***, ___).
// Inline: **strong**, *em*, ~~strike~~, `code`, [links](https://...), <https://autolinks>, bare URLs,
// citations [1], backslash escapes and hard breaks.

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'strong'; c: Inline[] }
  | { t: 'em'; c: Inline[] }
  | { t: 'strike'; c: Inline[] }
  | { t: 'code'; v: string }
  | { t: 'link'; href: string; c: Inline[] }
  | { t: 'cite'; n: number }
  | { t: 'br' };

export interface ListItem {
  /** The item's first paragraph (inline Markdown). */
  text: string;
  /** null for a plain item, a boolean for `- [ ]` / `- [x]`. */
  checked: boolean | null;
  /** Blocks nested in the item: sub-lists, more paragraphs, code. */
  children: Block[];
}

export type Align = 'left' | 'center' | 'right' | null;

export type Block =
  | { t: 'heading'; level: 1 | 2 | 3 | 4; text: string }
  | { t: 'para'; text: string }
  | { t: 'list'; ordered: boolean; start: number; items: ListItem[] }
  | { t: 'code'; lang: string; text: string }
  | { t: 'quote'; blocks: Block[] }
  | { t: 'table'; align: Align[]; head: string[]; rows: string[][] }
  | { t: 'hr' };

const LIST_RE = /^( *)([-*+]|\d{1,9}[.)])( +|$)(.*)$/;
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/;
const HEADING_RE = /^ {0,3}(#{1,6})(?:\s+(.*?))?\s*$/;
const HR_RE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE_RE = /^ {0,3}>/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

/** Text clean-up before parsing: line ends, tabs, and characters many fonts lack. */
export function normalizeMarkdown(md: string): string {
  return md
    .replace(/\r\n?/g, '\n')
    .replace(/\t/g, '    ')
    .replace(/[‐‑]/g, '-') // (non-breaking) hyphens: missing from many fonts, shown as boxes
    .replace(/[​﻿]/g, '');
}

function indentOf(line: string): number {
  return line.length - line.trimStart().length;
}

function isBlank(line: string | undefined): boolean {
  return line === undefined || !line.trim();
}

/** Splits a table row on unescaped pipes outside inline code; outer pipes are optional. */
export function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = '';
  let inCode = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === '\\' && s[i + 1] === '|') {
      cur += '|';
      i++;
    } else if (ch === '`') {
      inCode = !inCode;
      cur += ch;
    } else if (ch === '|' && !inCode) {
      cells.push(cur.trim());
      cur = '';
    } else {
      cur += ch;
    }
  }
  cells.push(cur.trim());
  return cells;
}

function isTableStart(lines: string[], i: number): boolean {
  const head = lines[i];
  const sep = lines[i + 1];
  if (sep === undefined || !head.includes('|') || !TABLE_SEP_RE.test(sep) || !sep.includes('-')) return false;
  if (!sep.includes('|') && splitRow(head).length < 2) return false;
  return splitRow(sep).length === splitRow(head).length || sep.includes('|');
}

/** Does this line start a block other than a paragraph (so it ends a paragraph or a lazy list line)? */
function startsBlock(lines: string[], i: number): boolean {
  const line = lines[i];
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    HR_RE.test(line) ||
    QUOTE_RE.test(line) ||
    LIST_RE.test(line) ||
    isTableStart(lines, i)
  );
}

function parseTable(lines: string[], i: number): { block: Block; next: number } {
  const head = splitRow(lines[i]);
  const align: Align[] = splitRow(lines[i + 1]).map((c) => {
    const l = c.startsWith(':');
    const r = c.endsWith(':');
    return l && r ? 'center' : r ? 'right' : l ? 'left' : null;
  });
  const width = head.length;
  const rows: string[][] = [];
  let j = i + 2;
  for (; j < lines.length; j++) {
    const line = lines[j];
    if (isBlank(line) || !line.includes('|') || (startsBlock(lines, j) && !line.trim().startsWith('|'))) break;
    const cells = splitRow(line);
    rows.push(Array.from({ length: width }, (_, k) => cells[k] ?? ''));
  }
  return { block: { t: 'table', align: Array.from({ length: width }, (_, k) => align[k] ?? null), head, rows }, next: j };
}

function parseList(lines: string[], i: number): { block: Block; next: number } {
  const first = LIST_RE.exec(lines[i])!;
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const bulletKind = ordered ? first[2].slice(-1) : first[2];
  const start = ordered ? parseInt(first[2], 10) : 1;
  const items: ListItem[] = [];
  let j = i;
  while (j < lines.length) {
    const m = LIST_RE.exec(lines[j]);
    // A sibling: same indentation (give or take one space) and the same kind of marker.
    if (!m || Math.abs(m[1].length - base) > 1) break;
    const isOrdered = /\d/.test(m[2]);
    if (isOrdered !== ordered || (isOrdered ? m[2].slice(-1) : m[2]) !== bulletKind) break;
    const gap = m[3].length;
    const contentCol = m[1].length + m[2].length + (gap > 4 || !m[4] ? 1 : gap);
    const body: string[] = [m[4]];
    j++;
    while (j < lines.length) {
      const line = lines[j];
      if (isBlank(line)) {
        // A blank line keeps the item open only if indented content follows.
        let k = j;
        while (k < lines.length && isBlank(lines[k])) k++;
        if (k < lines.length && indentOf(lines[k]) > base && indentOf(lines[k]) >= Math.min(contentCol, base + 2)) {
          for (; j < k; j++) body.push('');
          continue;
        }
        break;
      }
      const ind = indentOf(line);
      if (ind >= Math.min(contentCol, base + 2)) {
        body.push(line.slice(Math.min(ind, contentCol)));
        j++;
        continue;
      }
      // Lazy continuation of the item's paragraph ("- item\nmore text").
      if (!startsBlock(lines, j) && !isBlank(body[body.length - 1]) && !LIST_RE.test(body[body.length - 1])) {
        body.push(line.trim());
        j++;
        continue;
      }
      break;
    }
    const task = /^\[([ xX])\](?:\s+|$)(.*)$/.exec(body[0]);
    if (task) body[0] = task[2];
    const blocks = parseLines(body);
    let text = '';
    if (blocks[0]?.t === 'para') text = (blocks.shift() as { text: string }).text;
    items.push({ text, checked: task ? task[1] !== ' ' : null, children: blocks });
    // Blank lines between items keep one (loose) list.
    let k = j;
    while (k < lines.length && isBlank(lines[k])) k++;
    if (k > j && k < lines.length) {
      const n = LIST_RE.exec(lines[k]);
      if (n && Math.abs(n[1].length - base) <= 1 && /\d/.test(n[2]) === ordered) j = k;
    }
  }
  return { block: { t: 'list', ordered, start, items }, next: j };
}

function parseLines(lines: string[]): Block[] {
  const blocks: Block[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) blocks.push({ t: 'para', text: para.join('\n') });
    para = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE_RE.exec(line);
    if (fence) {
      flush();
      const marker = fence[1];
      const pad = indentOf(line);
      const code: string[] = [];
      for (i++; i < lines.length; i++) {
        const close = lines[i].trim();
        if (close.startsWith(marker[0].repeat(marker.length)) && /^[`~]+$/.test(close)) break;
        code.push(lines[i].slice(Math.min(pad, indentOf(lines[i]))));
      }
      blocks.push({ t: 'code', lang: fence[2] || '', text: code.join('\n') });
      i++;
      continue;
    }
    if (isBlank(line)) {
      flush();
      i++;
      continue;
    }
    const h = HEADING_RE.exec(line);
    if (h) {
      flush();
      const text = (h[2] ?? '').replace(/\s+#+\s*$/, '').replace(/^#+$/, '').trim();
      blocks.push({ t: 'heading', level: Math.min(h[1].length, 4) as 1 | 2 | 3 | 4, text });
      i++;
      continue;
    }
    if (HR_RE.test(line)) {
      flush();
      blocks.push({ t: 'hr' });
      i++;
      continue;
    }
    if (QUOTE_RE.test(line)) {
      flush();
      const quote: string[] = [];
      for (; i < lines.length; i++) {
        const q = lines[i];
        if (QUOTE_RE.test(q)) quote.push(q.replace(/^ {0,3}> ?/, ''));
        else if (!isBlank(q) && !startsBlock(lines, i) && quote.length && !isBlank(quote[quote.length - 1])) quote.push(q);
        else break;
      }
      blocks.push({ t: 'quote', blocks: parseLines(quote) });
      continue;
    }
    if (isTableStart(lines, i)) {
      flush();
      const { block, next } = parseTable(lines, i);
      blocks.push(block);
      i = next;
      continue;
    }
    if (LIST_RE.test(line) && (para.length === 0 || !/^\s*\d/.test(line) || /^\s*1[.)]/.test(line))) {
      flush();
      const { block, next } = parseList(lines, i);
      blocks.push(block);
      i = next;
      continue;
    }
    para.push(line.trim()); // every line break is kept, so trailing "  " hard breaks need no marker
    i++;
  }
  flush();
  return blocks;
}

export function parseBlocks(md: string): Block[] {
  return parseLines(normalizeMarkdown(md).split('\n'));
}

const SAFE_HREF = /^(https?:|mailto:)/i;
const ESCAPABLE = /^\\([\\`*_{}[\]()#+\-.!|~<>"'])/;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let buf = '';
  const pushText = () => {
    if (buf) out.push({ t: 'text', v: buf });
    buf = '';
  };
  let i = 0;
  while (i < text.length) {
    const rest = text.slice(i);
    let m: RegExpExecArray | null;
    if ((m = ESCAPABLE.exec(rest))) {
      buf += m[1];
    } else if ((m = /^(?: {2,}|\\)?\n/.exec(rest))) {
      buf = buf.replace(/ +$/, '');
      pushText();
      out.push({ t: 'br' });
    } else if ((m = /^(`+)([\s\S]*?[^`])\1(?!`)/.exec(rest))) {
      pushText();
      const v = m[2].replace(/\n/g, ' ');
      out.push({ t: 'code', v: /^ .* $/.test(v) && v.trim() ? v.slice(1, -1) : v });
    } else if ((m = /^\*\*\*(?!\s)([\s\S]+?)\*\*\*/.exec(rest))) {
      pushText();
      out.push({ t: 'strong', c: [{ t: 'em', c: parseInline(m[1]) }] });
    } else if ((m = /^\*\*(?!\s)([\s\S]+?)\*\*/.exec(rest)) || (m = /^__(?!\s)([\s\S]+?)__(?![A-Za-z0-9])/.exec(rest))) {
      pushText();
      out.push({ t: 'strong', c: parseInline(m[1]) });
    } else if ((m = /^~~(?!\s)([\s\S]+?)~~/.exec(rest))) {
      pushText();
      out.push({ t: 'strike', c: parseInline(m[1]) });
    } else if (
      (m = /^\*(?![\s*])([\s\S]+?)\*/.exec(rest)) ||
      ((i === 0 || !/[A-Za-z0-9]/.test(text[i - 1])) && (m = /^_(?![\s_])([\s\S]+?)_(?![A-Za-z0-9])/.exec(rest)))
    ) {
      pushText();
      out.push({ t: 'em', c: parseInline(m[1]) });
    } else if ((m = /^\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/.exec(rest))) {
      if (SAFE_HREF.test(m[2])) {
        pushText();
        out.push({ t: 'link', href: m[2], c: parseInline(m[1]) });
      } else {
        buf += m[1];
      }
    } else if ((m = /^\[(\d{1,3})\]/.exec(rest))) {
      pushText();
      out.push({ t: 'cite', n: Number(m[1]) });
    } else if ((m = /^<((?:https?:\/\/|mailto:)[^\s<>]+)>/i.exec(rest))) {
      pushText();
      out.push({ t: 'link', href: m[1], c: [{ t: 'text', v: m[1].replace(/^mailto:/i, '') }] });
    } else if ((m = /^https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"*_~]/.exec(rest)) && (i === 0 || /[\s(]/.test(text[i - 1]))) {
      pushText();
      out.push({ t: 'link', href: m[0], c: [{ t: 'text', v: m[0] }] });
    } else {
      buf += text[i];
      i++;
      continue;
    }
    i += m[0].length;
  }
  pushText();
  return out;
}

/** Plain-text preview of a note body: markup stripped, whitespace collapsed. */
export function plainText(md: string, max = 160): string {
  const text = normalizeMarkdown(md)
    .replace(/(```|~~~)[\s\S]*?(\1|$)/g, ' ')
    .replace(/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/gm, ' ')
    .replace(/^ {0,3}([-*_])(\s*\1){2,}\s*$/gm, ' ')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\s*\|\s*/g, ' ')
    .replace(/[*_`~]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/** Toggles the n-th task checkbox (`- [ ]`) in a Markdown body, in document order, skipping code blocks. */
export function toggleCheckbox(md: string, index: number): string {
  let seen = -1;
  let fence: string | null = null;
  return md
    .split('\n')
    .map((line) => {
      const f = /^ {0,3}(`{3,}|~{3,})/.exec(line);
      if (f) {
        if (fence === null) fence = f[1][0];
        else if (f[1][0] === fence && /^\s*[`~]+\s*$/.test(line)) fence = null;
        return line;
      }
      if (fence !== null) return line;
      return line.replace(/^(\s*(?:>\s*)*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\](?=\s|$))/, (all, a: string, mark: string, b: string) => {
        seen++;
        if (seen !== index) return all;
        return `${a}${mark === ' ' ? 'x' : ' '}${b}`;
      });
    })
    .join('\n');
}
