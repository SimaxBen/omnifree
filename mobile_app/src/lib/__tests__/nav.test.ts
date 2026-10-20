import { describe, expect, it, vi } from 'vitest';
import { announce, onTopic } from '../bus';
import { parseHash, routeHash } from '../nav';

describe('routes', () => {
  it('parses and builds hashes', () => {
    expect(parseHash('#/more/expenses')).toEqual({ tab: 'more', sub: 'expenses' });
    expect(parseHash('#/drive')).toEqual({ tab: 'drive', sub: '' });
    expect(parseHash('#/nope')).toBeNull();
    expect(parseHash('')).toBeNull();
    expect(routeHash({ tab: 'notes', sub: 'tasks' })).toBe('#/notes/tasks');
    expect(routeHash({ tab: 'home', sub: '' })).toBe('#/home');
  });
});

describe('bus', () => {
  it('delivers topics until unsubscribed', () => {
    const fn = vi.fn();
    const off = onTopic(fn);
    announce('notes', 'tasks');
    expect(fn.mock.calls).toEqual([['notes'], ['tasks']]);
    off();
    announce('notes');
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
