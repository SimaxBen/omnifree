// Global search helpers (GET /search): recent searches, accent-insensitive highlighting, icons per result type.
// No React, no I/O besides the guarded preference storage.

import type { IconName } from '../components/icons';
import { readPref, writePref } from './settings';
import type { SearchResponse, SearchResultType } from './types';

export const RECENT_KEY = 'search.recent';
export const MAX_RECENT = 8;
export const MIN_QUERY = 2;

export const RESULT_ICON: Record<SearchResultType, IconName> = {
  notes: 'note',
  tasks: 'check',
  expenses: 'receipt',
  incomes: 'cash',
  files: 'file',
  chats: 'chat',
  links: 'link',
};

/** Everything but the Drive: D1 scans that answer fast. The Drive listing (every backend) is asked separately so
 *  these groups show without waiting for it. */
export const FAST_TYPES: SearchResultType[] = ['notes', 'tasks', 'expenses', 'incomes', 'chats', 'links'];
export const SLOW_TYPES: SearchResultType[] = ['files'];

/** One response from the parts answered so far (groups ordered by their best hit, as the hub does). */
export function mergeSearch(parts: (SearchResponse | null | undefined)[]): SearchResponse | null {
  const got = parts.filter((p): p is SearchResponse => !!p);
  if (!got.length) return null;
  const groups = got.flatMap((p) => p.groups).sort((a, b) => (b.items[0]?.score ?? 0) - (a.items[0]?.score ?? 0));
  return {
    query: got[0].query,
    terms: got.find((p) => p.terms.length)?.terms ?? [],
    total: got.reduce((n, p) => n + p.total, 0),
    groups,
    errors: Object.assign({}, ...got.map((p) => p.errors)) as Record<string, string>,
  };
}

/** Lower case without accents ("Été" -> "ete"), one output character per input character when possible. */
export function fold(text: string): string {
  return text.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

/** Query words as the hub matches them (folded, de-duplicated). */
export function queryTerms(q: string): string[] {
  const out: string[] = [];
  for (const raw of fold(q).split(/[^\p{L}\p{N}@.+#_-]+/u)) {
    const t = raw.replace(/^[.-]+|[.-]+$/g, '');
    if (t && !out.includes(t)) out.push(t);
  }
  return out.slice(0, 8);
}

/** `q` first, de-duplicated (accent/case-insensitively), at most `max`. Blank queries leave the list as is. */
export function pushRecent(list: string[], q: string, max = MAX_RECENT): string[] {
  const query = q.trim().replace(/\s+/g, ' ');
  if (!query) return list;
  const key = fold(query);
  return [query, ...list.filter((x) => fold(x) !== key)].slice(0, max);
}

export function loadRecent(): string[] {
  const v = readPref<unknown>(RECENT_KEY, []);
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(0, MAX_RECENT) : [];
}

export function saveRecent(list: string[]): void {
  writePref(RECENT_KEY, list.slice(0, MAX_RECENT));
}

export interface TextPart {
  text: string;
  hit: boolean;
}

/** Splits `text` into plain and matching parts for highlighting (accent-insensitive, overlapping hits merged). */
export function highlightParts(text: string, terms: string[]): TextPart[] {
  if (!text) return [];
  // Fold character by character so indexes map back to `text` even where folding changes the length.
  const map: number[] = [];
  let folded = '';
  for (let i = 0; i < text.length; i++) {
    const f = fold(text[i]);
    folded += f;
    for (let j = 0; j < f.length; j++) map.push(i);
  }
  const marks = new Array<boolean>(text.length).fill(false);
  for (const term of terms.filter(Boolean)) {
    let from = 0;
    for (let at = folded.indexOf(term, from); at >= 0; at = folded.indexOf(term, from)) {
      const start = map[at];
      const end = map[at + term.length - 1];
      for (let k = start; k <= end; k++) marks[k] = true;
      from = at + term.length;
    }
  }
  const parts: TextPart[] = [];
  for (let i = 0; i < text.length; i++) {
    const last = parts[parts.length - 1];
    if (last && last.hit === marks[i]) last.text += text[i];
    else parts.push({ text: text[i], hit: marks[i] });
  }
  return parts;
}
