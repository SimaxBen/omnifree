// CSV / TSV for the spreadsheet grid: delimiter detection and an RFC 4180 parser (quotes, doubled
// quotes, CRLF, newlines inside quotes) that stops at a row cap.

export type Delimiter = ',' | ';' | '\t' | '|';

const CANDIDATES: Delimiter[] = [',', ';', '\t', '|'];

/** Counts each candidate per line outside quotes over the first lines; the most consistent wins. */
export function detectDelimiter(text: string, hint?: string): Delimiter {
  if (hint === 'tsv') return '\t';
  const sample = text.slice(0, 64 * 1024);
  const counts = new Map<Delimiter, number[]>(CANDIDATES.map((d) => [d, []]));
  let line = new Map<Delimiter, number>();
  let quoted = false;
  let lines = 0;
  const endLine = () => {
    for (const d of CANDIDATES) counts.get(d)!.push(line.get(d) ?? 0);
    line = new Map();
    lines++;
  };
  for (let i = 0; i < sample.length && lines < 50; i++) {
    const c = sample[i];
    if (c === '"') {
      quoted = !quoted;
    } else if (!quoted && (c === '\n' || c === '\r')) {
      if (c === '\r' && sample[i + 1] === '\n') i++;
      endLine();
    } else if (!quoted && (CANDIDATES as string[]).includes(c)) {
      line.set(c as Delimiter, (line.get(c as Delimiter) ?? 0) + 1);
    }
  }
  if (line.size) endLine();
  // Score: the share of lines holding the most common non-zero count. Near-ties go to the rarer
  // separators first (tab, semicolon, pipe, then comma), so "a;1,5" (decimal commas) stays ';'.
  let best: Delimiter = ',';
  let bestScore = 0;
  for (const d of ['\t', ';', '|', ','] as Delimiter[]) {
    const per = counts.get(d)!;
    const freq = new Map<number, number>();
    for (const n of per) if (n > 0) freq.set(n, (freq.get(n) ?? 0) + 1);
    if (!freq.size) continue;
    const times = Math.max(...freq.values());
    const score = times / per.length;
    if (score > bestScore + 0.05) {
      best = d;
      bestScore = score;
    }
  }
  return best;
}

export interface ParsedTable {
  rows: string[][];
  /** Rows were cut at `maxRows` (more data follows). */
  truncated: boolean;
  /** Widest row (before any column cap). */
  columns: number;
}

export function parseDelimited(text: string, delimiter: Delimiter, maxRows = 1000): ParsedTable {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  let columns = 0;
  let i = text.charCodeAt(0) === 0xfeff ? 1 : 0; // BOM
  const pushRow = () => {
    row.push(field);
    field = '';
    columns = Math.max(columns, row.length);
    rows.push(row);
    row = [];
  };
  for (; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          quoted = false;
        }
      } else {
        field += c;
      }
      continue;
    }
    if (c === '"' && field === '') {
      quoted = true;
    } else if (c === delimiter) {
      row.push(field);
      field = '';
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      pushRow();
      if (rows.length >= maxRows) {
        const rest = text.slice(i + 1);
        return { rows, truncated: rest.trim().length > 0, columns };
      }
    } else {
      field += c;
    }
  }
  if (field !== '' || row.length) pushRow();
  return { rows, truncated: false, columns };
}

/** A cell that reads as a number (right-aligned in the grid). */
export function looksNumeric(v: string): boolean {
  return /^\s*[-+]?(\d{1,3}([ ,. ]\d{3})*|\d+)([.,]\d+)?\s*%?\s*$/.test(v) && /\d/.test(v);
}
