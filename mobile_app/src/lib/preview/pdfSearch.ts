// Text search over pdf.js text items: case-insensitive, matches may span items (a line break counts
// as a space). Each match maps back to item segments so the viewer can draw highlight boxes.

export interface PdfTextItem {
  str: string;
  hasEOL?: boolean;
}

export interface MatchSegment {
  item: number;
  start: number;
  end: number;
}

export interface PdfMatch {
  /** 0-based page index. */
  page: number;
  segments: MatchSegment[];
}

function fold(s: string): string {
  // Lower-casing that keeps the length (so offsets still point into the original strings).
  let out = '';
  for (const ch of s) {
    const low = ch.toLowerCase();
    out += low.length === ch.length ? low : ch;
  }
  return out;
}

/**
 * Page text with every whitespace run folded to one space (items often end in a space and the
 * next starts with one, or hold double spaces), plus each folded character's offset in `text`.
 */
function collapse(text: string): { hay: string; at: number[] } {
  let hay = '';
  const at: number[] = [];
  for (let i = 0; i < text.length; i++) {
    const ws = /\s/.test(text[i]);
    if (ws && hay.endsWith(' ')) continue;
    hay += ws ? ' ' : text[i];
    at.push(i);
  }
  return { hay: fold(hay), at };
}

export function findInPages(pages: PdfTextItem[][], query: string, limit = 2000): PdfMatch[] {
  const q = fold(query.trim().replace(/\s+/g, ' '));
  if (!q) return [];
  const out: PdfMatch[] = [];
  for (let p = 0; p < pages.length && out.length < limit; p++) {
    const items = pages[p];
    let text = '';
    const starts: number[] = [];
    for (const it of items) {
      starts.push(text.length);
      text += it.str;
      if (it.hasEOL && !/\s$/.test(it.str)) text += ' ';
    }
    const { hay, at: map } = collapse(text);
    let found = hay.indexOf(q);
    while (found >= 0 && out.length < limit) {
      const at = map[found];
      const end = map[found + q.length - 1] + 1;
      const segments: MatchSegment[] = [];
      for (let i = 0; i < items.length; i++) {
        const s = starts[i];
        const e = s + items[i].str.length;
        const a = Math.max(s, at);
        const b = Math.min(e, end);
        if (a < b) segments.push({ item: i, start: a - s, end: b - s });
      }
      if (segments.length) out.push({ page: p, segments });
      found = hay.indexOf(q, found + q.length);
    }
  }
  return out;
}
