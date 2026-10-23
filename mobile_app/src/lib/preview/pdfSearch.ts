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
    const hay = fold(text.replace(/\s/g, ' '));
    let at = hay.indexOf(q);
    while (at >= 0 && out.length < limit) {
      const end = at + q.length;
      const segments: MatchSegment[] = [];
      for (let i = 0; i < items.length; i++) {
        const s = starts[i];
        const e = s + items[i].str.length;
        const a = Math.max(s, at);
        const b = Math.min(e, end);
        if (a < b) segments.push({ item: i, start: a - s, end: b - s });
      }
      if (segments.length) out.push({ page: p, segments });
      at = hay.indexOf(q, end);
    }
  }
  return out;
}
