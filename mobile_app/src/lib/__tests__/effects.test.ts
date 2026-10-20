import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { scrollToEnd } from '../dom';

// Regression for the production black screen ("TypeError: n is not a function" in React's commit
// phase): `useEffect(() => el.scrollIntoView(...))` leaked the Promise that recent Chromium returns
// from scrollIntoView, and React called it as the effect cleanup on the next commit.

describe('scrollToEnd', () => {
  it('never returns the Promise that scrollIntoView may return', () => {
    const rejected = Promise.reject(new Error('scroll interrupted'));
    const el = { scrollIntoView: vi.fn(() => rejected as unknown as void) };
    expect(scrollToEnd(el)).toBeUndefined();
    expect(el.scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'end' });
    expect(scrollToEnd(null)).toBeUndefined();
  });
});

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === '__tests__' ? [] : sources(path);
    return /\.tsx?$/.test(name) ? [path] : [];
  });
}

describe('effects', () => {
  it('use block bodies (or return a cleanup function), so no value leaks out as a cleanup', () => {
    const root = join(__dirname, '..', '..');
    const offenders: string[] = [];
    for (const file of sources(root)) {
      const text = readFileSync(file, 'utf8');
      // `useEffect(() => expr, ...)` where expr is neither a block nor another arrow (a cleanup).
      const re = /use(?:Layout)?Effect\(\s*(?:async\s*)?\(\)\s*=>(?!\s*(?:\{|\(\)\s*=>))/g;
      for (const m of text.matchAll(re)) {
        offenders.push(`${file}:${text.slice(0, m.index).split('\n').length}`);
      }
      if (/use(?:Layout)?Effect\(\s*async\b/.test(text)) offenders.push(`${file}: async effect`);
    }
    expect(offenders).toEqual([]);
  });
});
