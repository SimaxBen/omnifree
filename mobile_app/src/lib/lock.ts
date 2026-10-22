// App lock: a PIN (PBKDF2 hash) and/or the phone's fingerprint / face unlock (WebAuthn platform authenticator).
//
// This is a CONVENIENCE lock for a phone left unlocked, not authentication: the hub token stays in this
// browser's local storage and the hub never sees the PIN or the WebAuthn credential. The fingerprint check is a
// local "user present and verified" test (a WebAuthn get() with a challenge made here, the signature is not
// checked by any server). Forgetting the PIN is recovered by typing the hub token again.
//
// Everything is stored in localStorage (read synchronously at start, so a cold start shows the lock before any
// content); every access is guarded because private windows and blocked site data make storage throw.

export interface PinHash {
  /** base64 */
  salt: string;
  /** base64 PBKDF2-SHA256 output */
  hash: string;
  iterations: number;
  /** Number of digits, so the keypad checks as soon as the last one is typed. */
  length?: number;
}

export interface LockConfig {
  pin: PinHash | null;
  /** base64url credential id of the platform authenticator registered for this app. */
  credentialId: string | null;
  /** Minutes in the background before the app locks again (0 = as soon as it leaves the screen). */
  afterMinutes: number;
  /** Cover the screen when the app goes to the background (app switcher thumbnail). */
  shield: boolean;
}

export const LOCK_KEY = 'omnifree.lock.v1';
export const PIN_ITERATIONS = 210_000;
export const LOCK_AFTER_CHOICES = [0, 1, 5, 15, 60] as const;
export const MAX_FAILS = 5;
export const LOCKOUT_MS = 30_000;

export const DEFAULT_LOCK: LockConfig = { pin: null, credentialId: null, afterMinutes: 1, shield: true };

/** A lock is on when it has a PIN or a fingerprint credential. */
export function lockEnabled(cfg: LockConfig): boolean {
  return !!cfg.pin || !!cfg.credentialId;
}

export function isValidPin(pin: string): boolean {
  return /^\d{4,6}$/.test(pin);
}

export function parseLockConfig(raw: string | null): LockConfig {
  if (!raw) return { ...DEFAULT_LOCK };
  try {
    const v = JSON.parse(raw) as Partial<LockConfig>;
    const pin =
      v.pin && typeof v.pin.salt === 'string' && typeof v.pin.hash === 'string' && Number.isFinite(v.pin.iterations)
        ? { salt: v.pin.salt, hash: v.pin.hash, iterations: Number(v.pin.iterations), ...(Number(v.pin.length) >= 4 ? { length: Number(v.pin.length) } : {}) }
        : null;
    const after = Number(v.afterMinutes);
    return {
      pin,
      credentialId: typeof v.credentialId === 'string' && v.credentialId ? v.credentialId : null,
      afterMinutes: Number.isFinite(after) && after >= 0 ? Math.min(after, 24 * 60) : DEFAULT_LOCK.afterMinutes,
      shield: v.shield !== false,
    };
  } catch {
    return { ...DEFAULT_LOCK };
  }
}

export function loadLockConfig(): LockConfig {
  try {
    return parseLockConfig(window.localStorage.getItem(LOCK_KEY));
  } catch {
    return { ...DEFAULT_LOCK };
  }
}

/** Returns false when the browser refused to persist (the lock then only lasts for this session). */
export function saveLockConfig(cfg: LockConfig): boolean {
  try {
    window.localStorage.setItem(LOCK_KEY, JSON.stringify(cfg));
    return true;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------ encoding

export function toBase64(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

export function fromBase64(text: string): Uint8Array {
  const raw = atob(text);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function toBase64Url(bytes: Uint8Array): string {
  return toBase64(bytes).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function fromBase64Url(text: string): Uint8Array {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/');
  return fromBase64(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
}

export function randomBytes(n: number): Uint8Array {
  const out = new Uint8Array(n);
  crypto.getRandomValues(out);
  return out;
}

// ------------------------------------------------------------------ PIN

async function pbkdf2(pin: string, salt: Uint8Array, iterations: number): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(pin), 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: salt as BufferSource, iterations }, key, 256);
  return new Uint8Array(bits);
}

export async function hashPin(pin: string, iterations = PIN_ITERATIONS, salt: Uint8Array = randomBytes(16)): Promise<PinHash> {
  if (!isValidPin(pin)) throw new Error('The PIN must be 4 to 6 digits');
  return { salt: toBase64(salt), hash: toBase64(await pbkdf2(pin, salt, iterations)), iterations, length: pin.length };
}

/** Constant-time comparison of the derived key (the PIN itself is never stored). */
export async function verifyPin(pin: string, stored: PinHash): Promise<boolean> {
  if (!isValidPin(pin)) return false;
  const got = await pbkdf2(pin, fromBase64(stored.salt), stored.iterations);
  const want = fromBase64(stored.hash);
  if (got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++) diff |= got[i] ^ want[i];
  return diff === 0;
}

// ------------------------------------------------------------------ timing

/** Should the app lock when it comes back after being hidden since `hiddenAt` (ms)? */
export function shouldLockOnResume(cfg: LockConfig, hiddenAt: number | null, now: number): boolean {
  if (!lockEnabled(cfg) || hiddenAt === null) return false;
  return now - hiddenAt >= cfg.afterMinutes * 60_000;
}

/** Milliseconds left before another PIN attempt is allowed (after :data:`MAX_FAILS` wrong ones in a row). */
export function lockoutRemaining(fails: number, lastFailAt: number | null, now: number): number {
  if (fails < MAX_FAILS || lastFailAt === null) return 0;
  // Each further block of failures doubles the wait: 30 s, 60 s, 120 s ... capped at 15 min.
  const factor = 2 ** Math.min(Math.floor((fails - MAX_FAILS) / MAX_FAILS), 5);
  return Math.max(0, lastFailAt + Math.min(LOCKOUT_MS * factor, 15 * 60_000) - now);
}

/** "Forgot PIN": the typed hub token must match the stored one (whitespace ignored at the ends). */
export function tokenMatches(typed: string, stored: string): boolean {
  const a = typed.trim();
  const b = stored.trim();
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function describeLockAfter(minutes: number): string {
  if (minutes === 0) return 'Immediately';
  if (minutes < 60) return `After ${minutes} min`;
  return `After ${minutes / 60} h`;
}

// ------------------------------------------------------------------ fingerprint / face (WebAuthn)

/** A platform authenticator (fingerprint, face, device PIN) is available to this page. */
export async function biometricAvailable(): Promise<boolean> {
  try {
    const pkc = (window as unknown as { PublicKeyCredential?: { isUserVerifyingPlatformAuthenticatorAvailable?: () => Promise<boolean> } })
      .PublicKeyCredential;
    return !!pkc?.isUserVerifyingPlatformAuthenticatorAvailable && (await pkc.isUserVerifyingPlatformAuthenticatorAvailable());
  } catch {
    return false;
  }
}

/** Creates a credential on the phone's platform authenticator; returns its id (base64url). */
export async function registerBiometric(): Promise<string> {
  const cred = (await navigator.credentials.create({
    publicKey: {
      challenge: randomBytes(32) as BufferSource,
      rp: { name: 'OmniFree', id: window.location.hostname },
      user: { id: randomBytes(16) as BufferSource, name: 'owner', displayName: 'OmniFree owner' },
      pubKeyCredParams: [
        { type: 'public-key', alg: -7 },
        { type: 'public-key', alg: -257 },
      ],
      authenticatorSelection: { authenticatorAttachment: 'platform', userVerification: 'required', residentKey: 'discouraged' },
      timeout: 60_000,
      attestation: 'none',
    },
  })) as PublicKeyCredential | null;
  if (!cred) throw new Error('No credential was created');
  return toBase64Url(new Uint8Array(cred.rawId));
}

/** Asks for the fingerprint / face; resolves true when the authenticator verified the user. */
export async function verifyBiometric(credentialId: string): Promise<boolean> {
  try {
    const got = await navigator.credentials.get({
      publicKey: {
        challenge: randomBytes(32) as BufferSource,
        allowCredentials: [{ type: 'public-key', id: fromBase64Url(credentialId) as BufferSource, transports: ['internal'] }],
        userVerification: 'required',
        timeout: 60_000,
      },
    });
    return !!got;
  } catch {
    return false;
  }
}
