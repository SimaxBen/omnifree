import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useHub } from '../lib/hub';
import {
  DEFAULT_LOCK,
  MAX_FAILS,
  lockEnabled,
  lockoutRemaining,
  loadLockConfig,
  saveLockConfig,
  shouldLockOnResume,
  tokenMatches,
  verifyBiometric,
  verifyPin,
  type LockConfig,
} from '../lib/lock';
import { useNav } from '../lib/nav';
import { Icon } from './icons';
import { Button, cx, haptic, inputClass } from './ui';

// The app lock (see src/lib/lock.ts): locks on a cold start and after N minutes in the background, and covers
// the screen while the app is hidden (app switcher thumbnail). The app stays mounted under the lock screen, so a
// notification tap or a share that navigated while locked lands on its target right after unlocking.

interface LockValue {
  config: LockConfig;
  locked: boolean;
  /** Persists a new configuration (false when the browser refused to store it). */
  update: (cfg: LockConfig) => boolean;
  lockNow: () => void;
}

const LockContext = createContext<LockValue | null>(null);

export function useAppLock(): LockValue {
  const ctx = useContext(LockContext);
  if (!ctx) throw new Error('useAppLock must be used inside <AppLock>');
  return ctx;
}

export default function AppLock({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<LockConfig>(() => loadLockConfig());
  const [locked, setLocked] = useState(() => lockEnabled(config));
  const [hidden, setHidden] = useState(false);
  const hiddenAt = useRef<number | null>(null);
  const configRef = useRef(config);
  configRef.current = config;

  useEffect(() => {
    const onVisibility = () => {
      const cfg = configRef.current;
      if (document.visibilityState === 'hidden') {
        hiddenAt.current = Date.now();
        if (cfg.shield && lockEnabled(cfg)) setHidden(true);
        if (lockEnabled(cfg) && cfg.afterMinutes === 0) setLocked(true);
        return;
      }
      if (shouldLockOnResume(cfg, hiddenAt.current, Date.now())) setLocked(true);
      hiddenAt.current = null;
      setHidden(false);
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  const update = useCallback((cfg: LockConfig) => {
    setConfig(cfg);
    if (!lockEnabled(cfg)) setLocked(false);
    return saveLockConfig(cfg);
  }, []);

  const lockNow = useCallback(() => {
    if (lockEnabled(configRef.current)) setLocked(true);
  }, []);

  const value = useMemo(() => ({ config, locked, update, lockNow }), [config, locked, update, lockNow]);

  return (
    <LockContext.Provider value={value}>
      <div className="h-full" aria-hidden={locked || undefined}>
        {children}
      </div>
      {locked && <LockScreen config={config} onUnlock={() => setLocked(false)} onReset={() => update({ ...DEFAULT_LOCK, afterMinutes: config.afterMinutes, shield: config.shield })} />}
      {hidden && !locked && <Shield />}
    </LockContext.Provider>
  );
}

function Shield() {
  return (
    <div className="fixed inset-0 z-[200] grid place-items-center bg-ink-950" aria-hidden="true">
      <img src="/icons/icon.svg" alt="" className="h-16 w-16 opacity-80" />
    </div>
  );
}

const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'bio', '0', 'del'] as const;

function LockScreen({ config, onUnlock, onReset }: { config: LockConfig; onUnlock: () => void; onReset: () => void }) {
  const { settings, notify } = useHub();
  const { go } = useNav();
  const [pin, setPin] = useState('');
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fails, setFails] = useState(0);
  const [lastFail, setLastFail] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [recovering, setRecovering] = useState(false);
  const [token, setToken] = useState('');
  const triedBio = useRef(false);
  const length = config.pin?.length ?? 6;
  const wait = lockoutRemaining(fails, lastFail, now);

  useEffect(() => {
    if (!wait) return undefined;
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(t);
  }, [wait]);

  const bio = useCallback(async () => {
    if (!config.credentialId) return;
    setError(null);
    if (await verifyBiometric(config.credentialId)) {
      haptic(10);
      onUnlock();
    } else if (config.pin) {
      setError('Fingerprint not recognised: use your PIN');
    } else {
      setError('Fingerprint not recognised');
    }
  }, [config.credentialId, config.pin, onUnlock]);

  // Offer the fingerprint right away once (the PIN pad stays usable underneath).
  useEffect(() => {
    if (triedBio.current || !config.credentialId || document.visibilityState !== 'visible') return;
    triedBio.current = true;
    void bio();
  }, [bio, config.credentialId]);

  const check = async (value: string) => {
    if (!config.pin) return;
    setChecking(true);
    const ok = await verifyPin(value, config.pin);
    setChecking(false);
    if (ok) {
      haptic(10);
      onUnlock();
      return;
    }
    haptic([30, 40, 30]);
    const n = fails + 1;
    setFails(n);
    setLastFail(Date.now());
    setNow(Date.now());
    setPin('');
    setError(n >= MAX_FAILS ? 'Too many tries' : 'Wrong PIN');
  };

  const press = (k: (typeof KEYS)[number]) => {
    if (checking || wait) return;
    haptic(5);
    setError(null);
    if (k === 'bio') {
      void bio();
      return;
    }
    if (k === 'del') {
      setPin((p) => p.slice(0, -1));
      return;
    }
    const next = (pin + k).slice(0, 6);
    setPin(next);
    if (next.length === length) void check(next);
  };

  const recover = (e: FormEvent) => {
    e.preventDefault();
    // Without a saved token there is nothing on the hub this lock protects: any input removes it.
    if (settings.token.trim() && !tokenMatches(token, settings.token)) {
      setError('That is not the hub token saved in this app');
      return;
    }
    onReset();
    notify('Lock removed. Set a new PIN in Settings.', 'ok');
    go('more', 'settings');
  };

  return (
    <div className="pt-safe pb-safe fixed inset-0 z-[200] flex flex-col items-center bg-ink-950 px-6" role="dialog" aria-modal="true" aria-label="OmniFree is locked">
      <div className="flex w-full max-w-xs flex-1 flex-col items-center justify-center gap-6">
        <span className="grid h-16 w-16 place-items-center rounded-full bg-signal/15 text-signal">
          <Icon name="lock" size={30} />
        </span>
        <div className="text-center">
          <h1 className="font-display text-[26px] font-bold text-ink-100">OmniFree is locked</h1>
          <p className="pt-1 text-[14px] text-ink-400">{recovering ? 'Type your hub token to remove the lock.' : config.pin ? 'Enter your PIN' : 'Use your fingerprint or face'}</p>
        </div>

        {recovering ? (
          <form onSubmit={recover} className="w-full space-y-3">
            <input
              className={cx(inputClass, 'font-mono')}
              type="password"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              placeholder="HUB_API_TOKEN"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              aria-label="Hub token"
              autoFocus
            />
            {error && <p className="text-center text-[13px] text-danger-400">{error}</p>}
            <Button type="submit" variant="primary" className="w-full" disabled={!token.trim()}>
              Remove the lock
            </Button>
            <Button variant="ghost" className="w-full" onClick={() => { setRecovering(false); setError(null); }}>
              Back
            </Button>
          </form>
        ) : (
          <>
            {config.pin && (
              <div className={cx('flex h-6 items-center gap-3', error && 'shake')} aria-live="polite" aria-label={`${pin.length} of ${length} digits`}>
                {Array.from({ length }, (_, i) => (
                  <span key={i} className={cx('h-3.5 w-3.5 rounded-full transition-colors duration-fast', i < pin.length ? 'bg-signal' : 'bg-ink-700')} />
                ))}
              </div>
            )}
            <p className={cx('h-5 text-center text-[13px]', error ? 'text-danger-400' : 'text-ink-500')}>
              {wait ? `Try again in ${Math.ceil(wait / 1000)} s` : error ?? ''}
            </p>
            <div className="grid w-full grid-cols-3 gap-3">
              {KEYS.map((k) => {
                if (k === 'bio' && !config.credentialId) return <span key={k} />;
                if (k !== 'bio' && k !== 'del' && !config.pin) return <span key={k} />;
                if (k === 'del' && !config.pin) return <span key={k} />;
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => press(k)}
                    disabled={checking || (!!wait && k !== 'bio')}
                    aria-label={k === 'bio' ? 'Unlock with fingerprint or face' : k === 'del' ? 'Delete digit' : k}
                    className={cx(
                      'press grid h-[68px] place-items-center rounded-full text-[26px] font-semibold disabled:opacity-40',
                      k === 'bio' || k === 'del' ? 'text-ink-300' : 'bg-ink-850 text-ink-100 ring-1 ring-inset ring-ink-700 active:bg-ink-800',
                    )}
                  >
                    {k === 'bio' ? <Icon name="shield" size={26} /> : k === 'del' ? <Icon name="left" size={24} /> : k}
                  </button>
                );
              })}
            </div>
          </>
        )}
      </div>
      {!recovering && (
        <button type="button" onClick={() => { setRecovering(true); setError(null); }} className="press mb-6 min-h-[44px] px-4 text-[14px] text-ink-400 underline underline-offset-4">
          Forgot PIN?
        </button>
      )}
    </div>
  );
}
