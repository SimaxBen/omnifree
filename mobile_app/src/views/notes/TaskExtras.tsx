import { useState, type FormEvent } from 'react';
import { Icon } from '../../components/icons';
import { Button, Chip, IconButton, Spinner, cx, haptic, inputClass } from '../../components/ui';
import { clockTime, isoDay } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import {
  DAY_SHORT,
  ESTIMATES,
  activeBlock,
  addItem,
  checklistProgress,
  mergeAdded,
  minutesLabel,
  moveItem,
  removeItem,
  renameItem,
  slotLabel,
  toggleDay,
  toggleItem,
  type RepeatDraft,
  type RepeatKind,
} from '../../lib/tasks';
import type { ChecklistItem, Task, TaskSlots } from '../../lib/types';

const REPEAT_KINDS: { value: RepeatKind; label: string }[] = [
  { value: 'none', label: 'Never' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'interval', label: 'Every N days' },
  { value: 'yearly', label: 'Yearly' },
];

function SectionLabel({ children, aside }: { children: string; aside?: string }) {
  return (
    <span className="flex items-baseline justify-between px-1 text-[13px] text-ink-300">
      {children}
      {aside && <span className="text-[12px] text-ink-500">{aside}</span>}
    </span>
  );
}

/** "Repeat": never / daily / weekly on days / monthly on day N / every N days / yearly, with an optional end date. */
export function RepeatPicker({ value, onChange, label }: { value: RepeatDraft; onChange: (d: RepeatDraft) => void; label: string }) {
  const set = (patch: Partial<RepeatDraft>) => onChange({ ...value, ...patch });
  return (
    <div className="space-y-2">
      <SectionLabel aside={value.kind === 'none' ? undefined : label}>Repeat</SectionLabel>
      <div className="no-scrollbar -mx-5 flex gap-2 overflow-x-auto px-5">
        {REPEAT_KINDS.map((k) => (
          <Chip key={k.value} active={value.kind === k.value} onClick={() => set({ kind: k.value })}>
            {k.value !== 'none' && k.value === value.kind && <Icon name="repeat" size={14} />}
            {k.label}
          </Chip>
        ))}
      </div>
      {value.kind === 'weekly' && (
        <div className="flex justify-between gap-1" role="group" aria-label="Days of the week">
          {DAY_SHORT.map((d, i) => {
            const on = value.days.includes(i);
            return (
              <button
                key={d}
                type="button"
                aria-pressed={on}
                aria-label={d}
                onClick={() => {
                  haptic(5);
                  set({ days: toggleDay(value.days, i) });
                }}
                className={cx(
                  'press grid h-11 min-w-0 flex-1 place-items-center rounded-full text-[13px] font-semibold',
                  on ? 'bg-signal text-ink-950' : 'bg-ink-850 text-ink-300 ring-1 ring-inset ring-ink-700',
                )}
              >
                {d.slice(0, 2)}
              </button>
            );
          })}
        </div>
      )}
      {value.kind === 'monthly' && (
        <label className="flex items-center gap-3 px-1 text-[14px] text-ink-300">
          On day
          <input
            type="number"
            inputMode="numeric"
            min={1}
            max={31}
            aria-label="Day of the month"
            value={value.day}
            onChange={(e) => set({ day: Number(e.target.value) })}
            className={cx(inputClass, 'w-24')}
          />
          <span className="text-[12px] text-ink-500">short months use their last day</span>
        </label>
      )}
      {value.kind === 'interval' && (
        <label className="flex items-center gap-3 px-1 text-[14px] text-ink-300">
          Every
          <input
            type="number"
            inputMode="numeric"
            min={2}
            max={366}
            aria-label="Number of days"
            value={value.interval}
            onChange={(e) => set({ interval: Number(e.target.value) })}
            className={cx(inputClass, 'w-24')}
          />
          days
        </label>
      )}
      {value.kind !== 'none' && (
        <div className="flex items-center gap-2">
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">Repeat until</span>
            <input type="date" aria-label="Repeat until" value={value.until} onChange={(e) => set({ until: e.target.value })} className={cx(inputClass, !value.until && 'text-ink-500')} />
          </label>
          {value.until ? (
            <IconButton icon="x" label="Repeat forever" onClick={() => set({ until: '' })} />
          ) : (
            <span className="shrink-0 pr-1 text-[12px] text-ink-500">No end date</span>
          )}
        </div>
      )}
      {value.kind !== 'none' && <p className="px-1 text-[12px] text-ink-500">Completing it creates the next one.</p>}
    </div>
  );
}

/**
 * The checklist editor: check, rename in place, move up/down, remove, add. With a `taskId`, ticks, adds and removes
 * are saved at once (optimistic, like the task check; `onSaved` gets the hub's task); renames and order wait for
 * "Save task". Without one, every edit stays local.
 */
export function ChecklistEditor({
  items,
  onChange,
  taskId,
  onSaved,
}: {
  items: ChecklistItem[];
  onChange: (items: ChecklistItem[]) => void;
  taskId?: string;
  onSaved?: (t: Task) => void;
}) {
  const { client, notify } = useHub();
  const [text, setText] = useState('');
  const [adding, setAdding] = useState(false);
  const progress = checklistProgress(items);
  // Apply locally first, then save; on failure put the list back as it was.
  const persist = async (next: ChecklistItem[], call: () => Promise<Task>) => {
    const before = items;
    onChange(next);
    try {
      onSaved?.(await call());
    } catch (err) {
      onChange(before);
      notify(errorText(err), 'error');
    }
  };
  const toggle = (it: ChecklistItem) => {
    const next = toggleItem(items, it.id);
    if (!taskId) return onChange(next);
    void persist(next, () => client.updateChecklistItem(taskId, it.id, { done: !it.done }));
  };
  const remove = (it: ChecklistItem) => {
    const next = removeItem(items, it.id);
    if (!taskId) return onChange(next);
    void persist(next, () => client.deleteChecklistItem(taskId, it.id));
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const clean = text.replace(/\s+/g, ' ').trim();
    if (!clean) return;
    haptic(6);
    if (!taskId) {
      onChange(addItem(items, clean));
      setText('');
      return;
    }
    setAdding(true);
    try {
      const t = await client.addChecklistItem(taskId, clean);
      onChange(mergeAdded(items, t.checklist));
      setText('');
      onSaved?.(t);
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setAdding(false);
    }
  };
  return (
    <div className="space-y-2">
      <SectionLabel aside={progress.label ? `${progress.label} done` : undefined}>Checklist</SectionLabel>
      {items.length > 0 && (
        <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850 ring-1 ring-inset ring-ink-700">
          {items.map((it, i) => (
            <li key={it.id} className="flex items-center gap-1 pl-1 pr-1">
              <button
                type="button"
                role="checkbox"
                aria-checked={it.done}
                aria-label={`${it.done ? 'Uncheck' : 'Check'} ${it.text}`}
                onClick={() => {
                  haptic(it.done ? 5 : [6, 20, 10]);
                  toggle(it);
                }}
                className="press grid h-11 w-10 shrink-0 place-items-center"
              >
                <span className={cx('grid h-5 w-5 place-items-center rounded-[6px] ring-2 ring-inset', it.done ? 'bg-signal text-ink-950 ring-signal' : 'ring-ink-500')}>
                  {it.done && <Icon name="check" size={13} strokeWidth={3.2} />}
                </span>
              </button>
              <input
                value={it.text}
                aria-label={`Item ${i + 1}`}
                onChange={(e) => onChange(renameItem(items, it.id, e.target.value))}
                className={cx('min-h-[44px] min-w-0 flex-1 bg-transparent text-[15px] focus:outline-none', it.done ? 'text-ink-500 line-through' : 'text-ink-100')}
              />
              <IconButton icon="up" label={`Move ${it.text} up`} disabled={i === 0} onClick={() => onChange(moveItem(items, i, -1))} className="h-10 w-9" />
              <IconButton icon="down" label={`Move ${it.text} down`} disabled={i === items.length - 1} onClick={() => onChange(moveItem(items, i, 1))} className="h-10 w-9" />
              <IconButton icon="x" label={`Remove ${it.text}`} onClick={() => remove(it)} className="h-10 w-9 hover:text-danger-400" />
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={(e) => void add(e)} className="flex items-center gap-2">
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Add an item" aria-label="New checklist item" enterKeyHint="done" className={cx(inputClass, 'min-w-0 flex-1')} />
        <IconButton icon="plus" label="Add item" type="submit" disabled={!text.trim() || adding} className="bg-ink-800" />
      </form>
    </div>
  );
}

/**
 * "Time": an estimate plus "Block time", which books the task in the OmniFree Google Calendar at a suggested free slot
 * (09:00-20:00, around the agenda). Blocking acts at once (it does not wait for "Save task").
 */
export function TimeBlockPanel({
  task,
  estimate,
  onEstimate,
  onTask,
}: {
  task: Task;
  estimate: number | null;
  onEstimate: (m: number | null) => void;
  onTask: (t: Task) => void;
}) {
  const { client, notify } = useHub();
  const [slots, setSlots] = useState<TaskSlots | null>(null);
  const [loading, setLoading] = useState(false);
  const [booking, setBooking] = useState<string | null>(null);
  const today = isoDay();
  const block = activeBlock(task, `${today}T${clockTime()}`);

  const findSlots = async () => {
    setLoading(true);
    try {
      setSlots(await client.taskSlots(task.id, { days: 7, minutes: estimate ?? undefined }));
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setLoading(false);
    }
  };
  const book = async (start?: string) => {
    setBooking(start ?? 'auto');
    try {
      const res = await client.blockTask(task.id, { ...(start ? { start } : {}), ...(estimate ? { minutes: estimate } : {}) });
      haptic([8, 30, 14]);
      onTask(res.task);
      setSlots(null);
      announce('tasks');
      notify(`Blocked ${slotLabel(res.task.block ?? { start: null, end: null }, today)}`, 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBooking(null);
    }
  };
  const unblock = async () => {
    setBooking('remove');
    try {
      onTask(await client.unblockTask(task.id));
      announce('tasks');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBooking(null);
    }
  };

  return (
    <div className="space-y-2">
      <SectionLabel aside={estimate ? minutesLabel(estimate) : undefined}>Time needed</SectionLabel>
      <div className="no-scrollbar -mx-5 flex gap-2 overflow-x-auto px-5">
        {ESTIMATES.map((m) => (
          <Chip key={m} active={estimate === m} onClick={() => onEstimate(estimate === m ? null : m)}>
            {minutesLabel(m)}
          </Chip>
        ))}
      </div>

      {block ? (
        <div className="flex items-center gap-3 rounded-card bg-ink-850 px-3 py-2 ring-1 ring-inset ring-signal/40">
          <Icon name="clock" size={18} className="shrink-0 text-signal" />
          <span className="min-w-0 flex-1">
            <span className="block text-[15px] text-ink-100">{slotLabel(block, today)}</span>
            <span className="block text-[12px] text-ink-500">Blocked in Google Calendar</span>
          </span>
          <Button variant="ghost" onClick={() => void findSlots()} disabled={!!booking || loading} className="px-3">
            Move
          </Button>
          <Button variant="danger" onClick={() => void unblock()} disabled={!!booking} className="px-3">
            {booking === 'remove' ? <Spinner /> : 'Remove'}
          </Button>
        </div>
      ) : (
        !slots && (
          <Button icon="clock" className="w-full" onClick={() => void findSlots()} disabled={loading}>
            {loading ? 'Looking for free time…' : 'Block time'}
          </Button>
        )
      )}

      {slots && (
        <div className="screen-in-up space-y-2 rounded-card bg-ink-850 p-3 ring-1 ring-inset ring-ink-700">
          {!slots.writable ? (
            <p className="text-[13px] text-ink-400">Share the OmniFree Google Calendar first (More &gt; Notifications &gt; Google Calendar) to block time.</p>
          ) : slots.slots.length === 0 ? (
            <p className="text-[13px] text-ink-400">No free {minutesLabel(slots.minutes)} slot between {slots.work_hours[0]} and {slots.work_hours[1]} in the next 7 days.</p>
          ) : (
            <>
              <p className="text-[13px] text-ink-400">
                Free {minutesLabel(slots.minutes)} slots, {slots.work_hours[0]}–{slots.work_hours[1]}:
              </p>
              <div className="flex flex-wrap gap-2">
                {slots.slots.map((s) => (
                  <Chip key={s.start} onClick={() => void book(s.start)} active={booking === s.start}>
                    {booking === s.start ? <Spinner /> : <Icon name="calendar" size={14} />}
                    {slotLabel(s, today)}
                  </Chip>
                ))}
              </div>
            </>
          )}
          {slots.errors.length > 0 && <p className="text-[12px] text-amber-400">Could not read {slots.errors.map((e) => e.calendar).join(', ')}: those events were not checked.</p>}
          <button type="button" onClick={() => setSlots(null)} className="press min-h-[36px] text-[13px] text-ink-400 underline underline-offset-4">
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
