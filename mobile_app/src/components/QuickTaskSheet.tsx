import { useState, type FormEvent } from 'react';
import { addDays, isoDay } from '../lib/apps';
import { announce } from '../lib/bus';
import { errorText, useHub } from '../lib/hub';
import type { Task } from '../lib/types';
import { Sheet } from './Sheet';
import { Button, Chip, Field, inputClass } from './ui';

type When = 'none' | 'today' | 'tomorrow';

/** A one-field task capture (app icon shortcut "New task", Android share "Task"). */
export default function QuickTaskSheet({
  open,
  onClose,
  initialTitle = '',
  details,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  initialTitle?: string;
  details?: string;
  onCreated?: (task: Task) => void;
}) {
  const { client, notify } = useHub();
  const [title, setTitle] = useState(initialTitle);
  const [when, setWhen] = useState<When>('today');
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      const today = isoDay();
      const due = when === 'today' ? today : when === 'tomorrow' ? addDays(today, 1) : undefined;
      const task = await client.createTask({ title: title.trim().slice(0, 300), ...(details ? { details } : {}), ...(due ? { due } : {}) });
      announce('tasks');
      notify('Task added', 'ok');
      onCreated?.(task);
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open={open} onClose={onClose} title="New task">
      <form onSubmit={(e) => void submit(e)} className="space-y-4 pb-2">
        <Field label="Task">
          <input className={inputClass} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="What needs doing?" autoFocus enterKeyHint="done" />
        </Field>
        {details && <p className="line-clamp-2 break-all px-1 text-[13px] text-ink-400">{details}</p>}
        <div className="flex gap-2">
          {(['today', 'tomorrow', 'none'] as When[]).map((w) => (
            <Chip key={w} active={when === w} onClick={() => setWhen(w)}>
              {w === 'none' ? 'No date' : w === 'today' ? 'Today' : 'Tomorrow'}
            </Chip>
          ))}
        </div>
        <Button type="submit" variant="primary" className="w-full" disabled={busy || !title.trim()}>
          {busy ? 'Adding…' : 'Add task'}
        </Button>
      </form>
    </Sheet>
  );
}
