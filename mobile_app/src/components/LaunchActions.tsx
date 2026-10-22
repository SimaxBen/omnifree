import { useEffect, useRef, useState } from 'react';
import { useActions } from '../lib/actions';
import { parseLaunch, stripLaunch, type ShortcutId } from '../lib/launch';
import { useNav } from '../lib/nav';
import { useAppLock } from './AppLock';
import { Icon } from './icons';
import QuickTaskSheet from './QuickTaskSheet';
import { Sheet } from './Sheet';
import { Button } from './ui';

/**
 * Starts the capture asked for by an app icon shortcut (`/?shortcut=...`, see src/lib/launch.ts) once the app is
 * unlocked. The camera needs a tap (browsers only open a file picker from a user gesture), so "Scan receipt"
 * shows a one-button sheet instead of opening the camera by itself.
 */
export default function LaunchActions() {
  const actions = useActions();
  const { go } = useNav();
  const { locked } = useAppLock();
  const pending = useRef<ShortcutId | null>(null);
  const [receiptOpen, setReceiptOpen] = useState(false);
  const [taskOpen, setTaskOpen] = useState(false);

  useEffect(() => {
    const id = parseLaunch(window.location.search);
    if (!id) return;
    pending.current = id;
    try {
      const { pathname, search, hash } = window.location;
      window.history.replaceState(window.history.state, '', stripLaunch(pathname, search, hash));
    } catch {
      // The parameter just stays in the address bar.
    }
  }, []);

  useEffect(() => {
    const id = pending.current;
    if (locked || !id) return;
    pending.current = null;
    if (id === 'voice') actions.recordVoice();
    else if (id === 'note') actions.newNote();
    else if (id === 'task') setTaskOpen(true);
    else if (id === 'receipt') setReceiptOpen(true);
    else if (id === 'money') {
      go('more', 'money');
      actions.recordMoney();
    }
  }, [locked, actions, go]);

  return (
    <>
      <Sheet open={receiptOpen} onClose={() => setReceiptOpen(false)} title="Scan a receipt">
        <div className="space-y-3 pb-2">
          <p className="text-[14px] text-ink-400">Photograph the receipt: the merchant, total and items are read for you.</p>
          <Button
            variant="primary"
            className="w-full"
            onClick={() => {
              setReceiptOpen(false);
              actions.scanReceipt();
            }}
          >
            <Icon name="camera" size={18} /> Open the camera
          </Button>
        </div>
      </Sheet>
      {taskOpen && <QuickTaskSheet open={taskOpen} onClose={() => setTaskOpen(false)} />}
    </>
  );
}
