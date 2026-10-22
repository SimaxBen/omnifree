import { describe, expect, it } from 'vitest';
import {
  DEFAULT_LOCK,
  LOCKOUT_MS,
  MAX_FAILS,
  describeLockAfter,
  fromBase64Url,
  hashPin,
  isValidPin,
  lockEnabled,
  lockoutRemaining,
  parseLockConfig,
  shouldLockOnResume,
  toBase64Url,
  tokenMatches,
  verifyPin,
} from '../lock';

describe('app lock PIN', () => {
  it('accepts 4 to 6 digits only', () => {
    expect(['1234', '12345', '123456'].every(isValidPin)).toBe(true);
    expect(['123', '1234567', '12a4', ' 1234', ''].some(isValidPin)).toBe(false);
  });

  it('stores a salted PBKDF2 hash and verifies it', async () => {
    const h = await hashPin('2580', 1000);
    expect(h.iterations).toBe(1000);
    expect(h.length).toBe(4);
    expect(h.hash).not.toContain('2580');
    expect(await verifyPin('2580', h)).toBe(true);
    expect(await verifyPin('2581', h)).toBe(false);
    expect(await verifyPin('abcd', h)).toBe(false);
    const again = await hashPin('2580', 1000);
    expect(again.salt).not.toBe(h.salt); // a fresh salt each time
    expect(again.hash).not.toBe(h.hash);
    await expect(hashPin('12', 1000)).rejects.toThrow('4 to 6 digits');
  });

  it('parses stored configs defensively', () => {
    expect(parseLockConfig(null)).toEqual(DEFAULT_LOCK);
    expect(parseLockConfig('{nope')).toEqual(DEFAULT_LOCK);
    const stored = { pin: { salt: 's', hash: 'h', iterations: 5, length: 6 }, credentialId: 'abc', afterMinutes: 5, shield: false };
    expect(parseLockConfig(JSON.stringify(stored))).toEqual(stored);
    expect(parseLockConfig(JSON.stringify({ pin: { salt: 1 }, afterMinutes: -3 }))).toEqual(DEFAULT_LOCK);
    expect(lockEnabled(DEFAULT_LOCK)).toBe(false);
    expect(lockEnabled({ ...DEFAULT_LOCK, credentialId: 'x' })).toBe(true);
  });
});

describe('app lock timing', () => {
  const on = { ...DEFAULT_LOCK, pin: { salt: 's', hash: 'h', iterations: 1 }, afterMinutes: 5 };

  it('locks after the chosen time in the background', () => {
    expect(shouldLockOnResume(on, 0, 4 * 60_000)).toBe(false);
    expect(shouldLockOnResume(on, 0, 5 * 60_000)).toBe(true);
    expect(shouldLockOnResume({ ...on, afterMinutes: 0 }, 1000, 1000)).toBe(true);
    expect(shouldLockOnResume(on, null, 1e9)).toBe(false);
    expect(shouldLockOnResume(DEFAULT_LOCK, 0, 1e9)).toBe(false); // no PIN, no fingerprint: never
    expect(describeLockAfter(0)).toBe('Immediately');
    expect(describeLockAfter(15)).toBe('After 15 min');
    expect(describeLockAfter(60)).toBe('After 1 h');
  });

  it('slows down guessing after five wrong PINs', () => {
    expect(lockoutRemaining(MAX_FAILS - 1, 0, 0)).toBe(0);
    expect(lockoutRemaining(MAX_FAILS, 1000, 1000)).toBe(LOCKOUT_MS);
    expect(lockoutRemaining(MAX_FAILS, 1000, 1000 + LOCKOUT_MS)).toBe(0);
    expect(lockoutRemaining(MAX_FAILS * 2, 0, 0)).toBe(LOCKOUT_MS * 2);
    expect(lockoutRemaining(MAX_FAILS * 50, 0, 0)).toBe(15 * 60_000);
  });
});

describe('recovery and encoding', () => {
  it('matches the hub token exactly (ends trimmed)', () => {
    expect(tokenMatches(' tok_123 ', 'tok_123')).toBe(true);
    expect(tokenMatches('tok_124', 'tok_123')).toBe(false);
    expect(tokenMatches('', '')).toBe(false);
  });

  it('round-trips base64url credential ids', () => {
    const bytes = new Uint8Array([0, 255, 62, 63, 1, 2, 3]);
    const text = toBase64Url(bytes);
    expect(text).not.toMatch(/[+/=]/);
    expect(Array.from(fromBase64Url(text))).toEqual(Array.from(bytes));
  });
});
