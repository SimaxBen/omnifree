import { describe, expect, it } from 'vitest';
import { FOCUS_TTL_MS, peekFocus, requestFocus, takeFocus } from '../focus';
import { FAST_TYPES, SLOW_TYPES, mergeSearch } from '../search';
import type { SearchResponse } from '../types';

describe('focus requests', () => {
  it('waits for the target screen and is handed over once', () => {
    requestFocus({ kind: 'task', id: 't1', data: { title: 'x' } }, 1000);
    expect(peekFocus('link', 1000)).toBeNull();
    expect(peekFocus('task', 1500)).toEqual({ kind: 'task', id: 't1', data: { title: 'x' } });
    expect(takeFocus('task', 1500)?.id).toBe('t1');
    expect(takeFocus('task', 1500)).toBeNull();
  });

  it('keeps only the latest request per kind and drops stale ones', () => {
    requestFocus({ kind: 'link', id: 'a' }, 0);
    requestFocus({ kind: 'link', id: 'b' }, 10);
    expect(peekFocus('link', 20)?.id).toBe('b');
    expect(takeFocus('link', 10 + FOCUS_TTL_MS + 1)).toBeNull();
    expect(peekFocus('link', 20)).toBeNull();
  });
});

describe('split search', () => {
  const res = (type: 'notes' | 'files', score: number, errors: Record<string, string> = {}): SearchResponse => ({
    query: 'taj',
    terms: ['taj'],
    total: 2,
    groups: [{ type, label: type, count: 2, items: [{ type, id: type, title: type, snippet: '', date: null, score, data: {} }] }],
    errors,
  });

  it('covers every type once', () => {
    expect([...FAST_TYPES, ...SLOW_TYPES].sort()).toEqual(['chats', 'expenses', 'files', 'incomes', 'links', 'notes', 'tasks']);
  });

  it('merges the parts answered so far, best group first', () => {
    expect(mergeSearch([null, undefined])).toBeNull();
    const fastOnly = mergeSearch([res('notes', 3), null]);
    expect(fastOnly?.groups.map((g) => g.type)).toEqual(['notes']);
    const both = mergeSearch([res('notes', 3), res('files', 5, { files: 'timed out' })]);
    expect(both?.groups.map((g) => g.type)).toEqual(['files', 'notes']);
    expect(both?.total).toBe(4);
    expect(both?.terms).toEqual(['taj']);
    expect(both?.errors).toEqual({ files: 'timed out' });
  });
});
