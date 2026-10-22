import { describe, expect, it } from 'vitest';
import { MAX_RECENT, RESULT_ICON, fold, highlightParts, pushRecent, queryTerms } from '../search';

describe('global search helpers', () => {
  it('folds accents and case like the hub', () => {
    expect(fold('Été à FÈS')).toBe('ete a fes');
    expect(queryTerms('  Café, café  RÉSERVÉ!! ')).toEqual(['cafe', 'reserve']);
    expect(queryTerms('?!')).toEqual([]);
  });

  it('keeps recent searches unique, newest first, capped', () => {
    let list: string[] = [];
    for (let i = 0; i < 12; i++) list = pushRecent(list, `q${i}`);
    expect(list).toHaveLength(MAX_RECENT);
    expect(list[0]).toBe('q11');
    expect(pushRecent(['Café', 'tajine'], 'cafe ')).toEqual(['cafe', 'tajine']);
    expect(pushRecent(['a'], '   ')).toEqual(['a']);
    expect(pushRecent([], '  two   words ')).toEqual(['two words']);
  });

  it('highlights matches accent-insensitively and merges overlaps', () => {
    expect(highlightParts('Le Café de Fès', ['cafe', 'fes'])).toEqual([
      { text: 'Le ', hit: false },
      { text: 'Café', hit: true },
      { text: ' de ', hit: false },
      { text: 'Fès', hit: true },
    ]);
    expect(highlightParts('tajine tajine', ['taj', 'jine'])).toEqual([
      { text: 'tajine', hit: true },
      { text: ' ', hit: false },
      { text: 'tajine', hit: true },
    ]);
    expect(highlightParts('rien', ['x'])).toEqual([{ text: 'rien', hit: false }]);
    expect(highlightParts('', ['x'])).toEqual([]);
  });

  it('has an icon for every result type', () => {
    expect(Object.keys(RESULT_ICON).sort()).toEqual(['chats', 'expenses', 'files', 'incomes', 'links', 'notes', 'tasks']);
  });
});
