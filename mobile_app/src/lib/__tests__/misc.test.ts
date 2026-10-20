import { describe, expect, it } from 'vitest';
import { matchesPattern } from '../events';
import { formatBytes, fraction } from '../format';
import { moveItem } from '../list';

describe('matchesPattern', () => {
  it('follows core/events.py pattern rules', () => {
    expect(matchesPattern('router.failover', 'router.*')).toBe(true);
    expect(matchesPattern('routerx.failover', 'router.*')).toBe(false);
    expect(matchesPattern('instinct.task.screenshot', 'instinct.*')).toBe(true);
    expect(matchesPattern('quota.updated', '*')).toBe(true);
    expect(matchesPattern('quota.updated', 'quota.exhausted')).toBe(false);
  });
});

describe('moveItem', () => {
  it('moves and clamps', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a']);
    expect(moveItem(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b']);
    expect(moveItem(['a', 'b'], 0, 9)).toEqual(['b', 'a']);
    expect(moveItem(['a', 'b'], -1, 0)).toEqual(['a', 'b']);
  });
});

describe('format', () => {
  it('formats bytes and fractions', () => {
    expect(formatBytes(10e9)).toBe('10.0 GB');
    expect(formatBytes(512)).toBe('512 B');
    expect(fraction(5, 10)).toBe(0.5);
    expect(fraction(5, null)).toBeNull();
    expect(fraction(20, 10)).toBe(1);
  });
});
