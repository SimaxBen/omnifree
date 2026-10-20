// A deliberately small Markdown subset for notes and Ask-my-Drive answers. It produces tokens that
// the <Markdown> component renders as React elements, so no HTML string is ever injected.

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'strong'; c: Inline[] }
  | { t: 'em'; c: Inline[] }
  | { t: 'code'; v: string }
  | { t: 'link'; href: string; c: Inline[] }
  | { t: 'cite'; n: number };

export interface ListItem {
  text: string;
  /** null for a plain bullet, a boolean for `- [ ]` / `- [x]`. */
  checked: boolean | null;
}

export type Block =
  | { t: 'heading'; level: 1 | 2 | 3; text: string }
  | { t: 'para'; text: string }
  | { t: 'list'; ordered: boolean; items: ListItem[] }
  | { t: 'code'; text: string }
  | { t: 'quote'; text: string }
  | { t: 'hr' };

const LIST_RE = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/;

export function parseBlocks(md: string): Block[] {
  const lines = md.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) blocks.push({ t: 'para', text: para.join('\n') });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^```/.test(line.trim())) {
      flush();
      const code: string[] = [];
      for (i++; i < lines.length && !/^```/.test(lines[i].trim()); i++) code.push(lines[i]);
      blocks.push({ t: 'code', text: code.join('\n') });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flush();
      blocks.push({ t: 'heading', level: Math.min(h[1].length, 3) as 1 | 2 | 3, text: h[2].trim() });
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      blocks.push({ t: 'hr' });
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      const quote: string[] = [];
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ''));
      i--;
      blocks.push({ t: 'quote', text: quote.join('\n') });
      continue;
    }
    const li = LIST_RE.exec(line);
    if (li) {
      flush();
      const ordered = !li[1];
      const items: ListItem[] = [];
      for (; i < lines.length; i++) {
        const m = LIST_RE.exec(lines[i]);
        if (!m || !m[1] !== ordered) break;
        const task = /^\[([ xX])\]\s+(.*)$/.exec(m[3]);
        items.push(task ? { text: task[2], checked: task[1] !== ' ' } : { text: m[3], checked: null });
      }
      i--;
      blocks.push({ t: 'list', ordered, items });
      continue;
    }
    para.push(line);
  }
  flush();
  return blocks;
}

const SAFE_HREF = /^(https?:|mailto:)/i;

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
    if ((m = /^`([^`]+)`/.exec(rest))) {
      pushText();
      out.push({ t: 'code', v: m[1] });
    } else if ((m = /^\*\*(.+?)\*\*/.exec(rest)) || (m = /^__(.+?)__/.exec(rest))) {
      pushText();
      out.push({ t: 'strong', c: parseInline(m[1]) });
    } else if ((m = /^\*(?!\s)(.+?)\*/.exec(rest)) || (m = /^_(?!\s)(.+?)_(?![A-Za-z0-9])/.exec(rest))) {
      pushText();
      out.push({ t: 'em', c: parseInline(m[1]) });
    } else if ((m = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest))) {
      if (SAFE_HREF.test(m[2])) {
        pushText();
        out.push({ t: 'link', href: m[2], c: parseInline(m[1]) });
      } else {
        buf += m[1];
      }
    } else if ((m = /^\[(\d{1,3})\]/.exec(rest))) {
      pushText();
      out.push({ t: 'cite', n: Number(m[1]) });
    } else if ((m = /^https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/.exec(rest)) && (i === 0 || /\s|\(/.test(text[i - 1]))) {
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
  const text = md
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/[*_`]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/** Toggles the n-th task checkbox (`- [ ]`) in a Markdown body. */
export function toggleCheckbox(md: string, index: number): string {
  let seen = -1;
  return md.replace(/^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\])/gm, (all, a: string, mark: string, b: string) => {
    seen++;
    if (seen !== index) return all;
    return `${a}${mark === ' ' ? 'x' : ' '}${b}`;
  });
}
