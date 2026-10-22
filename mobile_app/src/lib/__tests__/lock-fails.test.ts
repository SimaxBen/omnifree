import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  FAILS_KEY,
  RETURN_GRACE_MS,
  clearFails,
  expectReturn,
  expectingReturn,
  loadFailState,
  parseFailState,
  recordFail,
  takeExpectedReturn,
} from '../lock';

describe('persisted PIN failures', () => {
  const store = new Map<string, string>();
  vi.stubGlobal('window', {
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    },
  });
  afterEach(() => store.clear());

  it('parses defensively', () => {
    expect(parseFailState(null)).toEqual({ fails: 0, lastFailAt: null });
    expect(parseFailState('nope')).toEqual({ fails: 0, lastFailAt: null });
    expect(parseFailState('{"fails":-3,"lastFailAt":"x"}')).toEqual({ fails: 0, lastFailAt: null });
    expect(parseFailState('{"fails":7,"lastFailAt":1000}')).toEqual({ fails: 7, lastFailAt: 1000 });
  });

  it('counts across reloads and clears on success', () => {
    expect(loadFailState()).toEqual({ fails: 0, lastFailAt: null });
    recordFail(1000);
    expect(recordFail(2000)).toEqual({ fails: 2, lastFailAt: 2000 });
    expect(JSON.parse(store.get(FAILS_KEY) ?? '{}')).toEqual({ fails: 2, lastFailAt: 2000 });
    expect(loadFailState()).toEqual({ fails: 2, lastFailAt: 2000 }); // what a cold start reads
    clearFails();
    expect(store.has(FAILS_KEY)).toBe(false);
  });

  it('survives blocked storage', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: () => {
          throw new Error('blocked');
        },
        setItem: () => {
          throw new Error('blocked');
        },
        removeItem: () => {
          throw new Error('blocked');
        },
      },
    });
    expect(loadFailState()).toEqual({ fails: 0, lastFailAt: null });
    expect(recordFail(5)).toEqual({ fails: 1, lastFailAt: 5 });
    expect(() => clearFails()).not.toThrow();
  });
});

describe('own camera / picker grace', () => {
  it('skips one lock within the grace window only', () => {
    expect(takeExpectedReturn(1000)).toBe(false);
    expectReturn(1000);
    expect(expectingReturn(1500)).toBe(true);
    expect(takeExpectedReturn(1000 + RETURN_GRACE_MS - 1)).toBe(true);
    expect(takeExpectedReturn(1000 + RETURN_GRACE_MS - 1)).toBe(false); // consumed
    expectReturn(1000);
    expect(expectingReturn(1000 + RETURN_GRACE_MS)).toBe(false);
    expect(takeExpectedReturn(1000 + RETURN_GRACE_MS)).toBe(false);
  });
});
