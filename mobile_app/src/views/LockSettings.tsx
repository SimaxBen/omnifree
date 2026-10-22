import { useEffect, useState, type FormEvent } from 'react';
import { useAppLock } from '../components/AppLock';
import { Badge, Button, Card, Chip, SectionTitle, Toggle, cx, inputClass } from '../components/ui';
import { errorText, useHub } from '../lib/hub';
import { LOCK_AFTER_CHOICES, biometricAvailable, describeLockAfter, hashPin, isValidPin, lockEnabled, registerBiometric } from '../lib/lock';

/** Settings section for the app lock (PIN, fingerprint / face, timeout, app switcher cover). */
export default function LockSettings() {
  const { config, update, lockNow } = useAppLock();
  const { notify } = useHub();
  const [editing, setEditing] = useState(false);
  const [pin, setPin] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [bioOk, setBioOk] = useState<boolean | null>(null);
  const on = lockEnabled(config);

  useEffect(() => {
    void biometricAvailable().then(setBioOk);
  }, []);

  const save = (next: typeof config, message: string) => {
    const persisted = update(next);
    notify(persisted ? message : 'Saved for this session only: this browser blocks local storage', persisted ? 'ok' : 'error');
  };

  const savePin = async (e: FormEvent) => {
    e.preventDefault();
    if (!isValidPin(pin)) return notify('The PIN must be 4 to 6 digits', 'error');
    if (pin !== confirm) return notify('The two PINs differ', 'error');
    setBusy(true);
    try {
      save({ ...config, pin: await hashPin(pin) }, config.pin ? 'PIN changed' : 'App lock on');
      setEditing(false);
      setPin('');
      setConfirm('');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  const toggleBio = async (enable: boolean) => {
    if (!enable) {
      save({ ...config, credentialId: null }, 'Fingerprint unlock off');
      return;
    }
    setBusy(true);
    try {
      save({ ...config, credentialId: await registerBiometric() }, 'Fingerprint unlock on');
    } catch (err) {
      notify(`Fingerprint setup failed: ${errorText(err)}`, 'error');
    } finally {
      setBusy(false);
    }
  };

  const digits = (v: string) => v.replace(/\D/g, '').slice(0, 6);

  return (
    <div>
      <SectionTitle action={<Badge tone={on ? 'ok' : 'neutral'}>{on ? 'On' : 'Off'}</Badge>}>App lock</SectionTitle>
      <Card className="space-y-4 p-3">
        {editing ? (
          <form onSubmit={(e) => void savePin(e)} className="space-y-3">
            <label className="block space-y-1 text-sm text-ink-200">
              New PIN (4 to 6 digits)
              <input className={cx(inputClass, 'font-mono tracking-[0.4em]')} type="password" inputMode="numeric" autoComplete="off" value={pin} onChange={(e) => setPin(digits(e.target.value))} autoFocus />
            </label>
            <label className="block space-y-1 text-sm text-ink-200">
              Same PIN again
              <input className={cx(inputClass, 'font-mono tracking-[0.4em]')} type="password" inputMode="numeric" autoComplete="off" value={confirm} onChange={(e) => setConfirm(digits(e.target.value))} />
            </label>
            <div className="flex gap-2">
              <Button type="submit" variant="primary" className="flex-1" disabled={busy || !isValidPin(pin) || pin !== confirm}>
                {busy ? 'Saving…' : 'Save PIN'}
              </Button>
              <Button variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button icon="lock" onClick={() => setEditing(true)}>
              {config.pin ? 'Change PIN' : 'Set a PIN'}
            </Button>
            {config.pin && (
              <Button variant="danger" onClick={() => save({ ...config, pin: null }, config.credentialId ? 'PIN removed' : 'App lock off')}>
                Remove PIN
              </Button>
            )}
            {on && (
              <Button variant="ghost" onClick={lockNow}>
                Lock now
              </Button>
            )}
          </div>
        )}

        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[15px] text-ink-100">Fingerprint or face</p>
            <p className="text-[13px] text-ink-400">{bioOk === false ? 'Not available in this browser' : 'Unlock with the phone’s own biometrics'}</p>
          </div>
          <Toggle label="Fingerprint or face unlock" checked={!!config.credentialId} disabled={busy || bioOk === false} onChange={(v) => void toggleBio(v)} />
        </div>

        <div className="space-y-2">
          <p className="text-[15px] text-ink-100">Lock when the app is in the background</p>
          <div className="no-scrollbar -mx-1 flex gap-2 overflow-x-auto px-1">
            {LOCK_AFTER_CHOICES.map((m) => (
              <Chip key={m} active={config.afterMinutes === m} onClick={() => save({ ...config, afterMinutes: m }, `Locks ${describeLockAfter(m).toLowerCase()}`)}>
                {describeLockAfter(m)}
              </Chip>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[15px] text-ink-100">Hide in the app switcher</p>
            <p className="text-[13px] text-ink-400">Covers the screen when you leave the app (lock on)</p>
          </div>
          <Toggle label="Hide content in the app switcher" checked={config.shield} onChange={(v) => save({ ...config, shield: v }, v ? 'Content hidden in the app switcher' : 'App switcher cover off')} />
        </div>

        <p className="text-xs text-ink-500">
          A convenience lock for this phone: the PIN never leaves it (only a PBKDF2 hash is kept here) and the fingerprint check happens on the device. It
          does not protect the hub itself. Forgot the PIN? The lock screen removes it when you type the hub token again.
        </p>
      </Card>
    </div>
  );
}
