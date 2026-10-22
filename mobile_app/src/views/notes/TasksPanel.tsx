import { useMemo, useRef, useState, type FormEvent } from 'react';
import { Icon } from '../../components/icons';
import { Sheet, useConfirm } from '../../components/Sheet';
import { SwipeRow } from '../../components/SwipeRow';
import { Button, Chip, EmptyState, ErrorNote, Field, IconButton, Segmented, SkeletonList, cx, haptic, inputClass, riseStyle } from '../../components/ui';
import { eventWhen, groupEventsByDay } from '../../lib/agenda';
import { addDays, dueInfo, isoDay, sortTasks, taskDue, validTime } from '../../lib/apps';
import { cleanChecklist, recurrenceLabel, repeatDraft, ruleFromDraft } from '../../lib/tasks';
import { announce, useTopic } from '../../lib/bus';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { usePullRefresh } from '../../lib/nav';
import type { ChecklistItem, Task, TaskPriority } from '../../lib/types';
import { ChecklistEditor, RepeatPicker, TimeBlockPanel } from './TaskExtras';
import { TaskRow } from './TaskRow';

function TaskSheet({ open, task, onClose, onSaved, onDeleted }: { open: boolean; task: Task; onClose: () => void; onSaved: (t: Task) => void; onDeleted: (id: string) => void }) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [title, setTitle] = useState(task.title);
  const [details, setDetails] = useState(task.details ?? '');
  const [due, setDueRaw] = useState(task.due ?? '');
  const [time, setTimeRaw] = useState(validTime(task.due_time) ?? '');
  const [priority, setPriority] = useState<TaskPriority>(task.priority);
  const [repeat, setRepeat] = useState(() => repeatDraft(task.recurrence, task.due, isoDay()));
  const [checklist, setChecklist] = useState<ChecklistItem[]>(task.checklist ?? []);
  const [estimate, setEstimate] = useState<number | null>(task.estimate ?? null);
  const [live, setLive] = useState<Task>(task); // time blocks are booked at once, outside "Save task"
  const [busy, setBusy] = useState(false);
  // A time needs a day: picking one with no date means today; clearing the date clears the time.
  const setDue = (d: string) => {
    setDueRaw(d);
    if (!d) setTimeRaw('');
  };
  const setTime = (t: string) => {
    setTimeRaw(t);
    if (t && !due) setDueRaw(isoDay());
  };

  const save = async () => {
    setBusy(true);
    try {
      const at = due ? validTime(time) : null;
      const recurrence = ruleFromDraft(repeat, due || null, isoDay());
      const t = await client.updateTask(task.id, {
        title: title.trim() || task.title,
        details: details.trim() || null,
        // a repeating task with no date gets its first occurrence from the hub
        due: due || null,
        due_time: due || !recurrence ? at : validTime(time),
        priority,
        recurrence,
        checklist: cleanChecklist(checklist),
        estimate,
      });
      onSaved(t);
      announce('tasks');
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!(await confirm({ title: 'Delete this task?', body: task.title, confirmLabel: 'Delete task', tone: 'danger' }))) return;
    try {
      await client.deleteTask(task.id);
      onDeleted(task.id);
      announce('tasks');
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };
  const today = isoDay();
  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Task"
      headerAction={<IconButton icon="trash" label="Delete task" onClick={() => void remove()} className="hover:text-danger-400" />}
      footer={
        <Button variant="primary" className="w-full" onClick={() => void save()} disabled={busy}>
          Save task
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="What">
          <input value={title} onChange={(e) => setTitle(e.target.value)} className={inputClass} />
        </Field>
        <Field label="Details">
          <textarea value={details} onChange={(e) => setDetails(e.target.value)} placeholder="Optional" className={cx(inputClass, 'min-h-[88px] py-3')} />
        </Field>
        <div className="space-y-1.5">
          <span className="block px-1 text-[13px] text-ink-300">Due</span>
          <div className="flex flex-wrap gap-2">
            <Chip active={due === today} onClick={() => setDue(today)}>
              Today
            </Chip>
            <Chip active={due === addDays(today, 1)} onClick={() => setDue(addDays(today, 1))}>
              Tomorrow
            </Chip>
            <Chip active={due === addDays(today, 7)} onClick={() => setDue(addDays(today, 7))}>
              In a week
            </Chip>
            <Chip active={!due} onClick={() => setDue('')}>
              No date
            </Chip>
          </div>
          <input type="date" aria-label="Due date" value={due} onChange={(e) => setDue(e.target.value)} className={inputClass} />
        </div>
        <div className="space-y-1.5">
          <span className="block px-1 text-[13px] text-ink-300">Time</span>
          <div className="flex items-center gap-2">
            <input
              type="time"
              aria-label="Due time"
              value={time}
              step={300}
              onChange={(e) => setTime(e.target.value)}
              className={cx(inputClass, 'min-w-0 flex-1', !time && 'text-ink-500')}
            />
            {time && (
              <IconButton icon="x" label="Clear time" onClick={() => setTimeRaw('')} className="h-11 w-11 shrink-0" />
            )}
          </div>
          <p className="px-1 text-[12px] text-ink-500">{time ? 'A reminder is pushed 15 min before.' : 'Optional: all day without a time.'}</p>
        </div>
        <RepeatPicker value={repeat} onChange={setRepeat} label={recurrenceLabel(ruleFromDraft(repeat, due || null, today))} />
        <ChecklistEditor
          items={checklist}
          onChange={setChecklist}
          taskId={task.id}
          onSaved={(t) => {
            setLive(t);
            onSaved(t);
            announce('tasks');
          }}
        />
        <TimeBlockPanel
          task={live}
          estimate={estimate}
          onEstimate={setEstimate}
          onTask={(t) => {
            setLive(t);
            onSaved(t);
          }}
        />
        <div className="space-y-1.5">
          <span className="block px-1 text-[13px] text-ink-300">Priority</span>
          <Segmented
            value={priority}
            onChange={setPriority}
            options={[
              { value: 'low', label: 'Low' },
              { value: 'normal', label: 'Normal' },
              { value: 'high', label: 'High' },
            ]}
          />
        </div>
      </div>
    </Sheet>
  );
}

const EVENTS_OPEN_KEY = 'omnifree.tasks.eventsOpen';

function readOpen(): boolean {
  try {
    return window.localStorage.getItem(EVENTS_OPEN_KEY) !== '0';
  } catch {
    return true;
  }
}

/** "Upcoming events": the next 7 days of Google Calendar (OmniFree's own task events are left out by the hub). */
function UpcomingEvents() {
  const events = useHubData((c, s) => c.gcalEvents(7, s), [], 5 * 60_000);
  const [open, setOpen] = useState(readOpen);
  const today = isoDay();
  const groups = useMemo(() => groupEventsByDay(events.data?.events ?? [], today, 7), [events.data, today]);
  usePullRefresh(events.reload);
  if (!events.data?.configured) return null;
  const count = groups.reduce((n, g) => n + g.events.length, 0);
  const toggle = () => {
    setOpen((v) => {
      try {
        window.localStorage.setItem(EVENTS_OPEN_KEY, v ? '0' : '1');
      } catch {
        /* private mode: not remembered */
      }
      return !v;
    });
  };
  return (
    <section className="space-y-2">
      <button type="button" onClick={toggle} aria-expanded={open} className="press flex min-h-[44px] w-full items-center gap-1.5 px-1 text-left">
        <Icon name="calendar" size={16} className="text-signal" />
        <span className="font-display text-[15px] font-semibold text-ink-200">Upcoming events</span>
        <span className="text-[15px] text-ink-500">{count}</span>
        <Icon name={open ? 'up' : 'down'} size={16} className="ml-auto text-ink-500" />
      </button>
      {open && (
        <div className="screen-in-up space-y-3">
          {events.data.errors.length > 0 && (
            <p className="px-1 text-[12px] text-amber-400">Could not read {events.data.errors.map((e) => e.calendar).join(', ')}: check the sharing in Notifications &gt; Google Calendar.</p>
          )}
          {count === 0 && <p className="px-1 text-[14px] text-ink-500">Nothing on the calendar this week.</p>}
          {groups.map((g) => (
            <div key={g.day} className="space-y-1.5">
              <h3 className={cx('px-1 text-[13px] font-semibold', g.label === 'Today' ? 'text-signal' : 'text-ink-400')}>{g.label}</h3>
              <div className="divide-y divide-ink-800 rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
                {g.events.map((ev) => (
                  <a
                    key={`${ev.calendar_id}:${ev.id}:${g.day}`}
                    href={ev.link ?? undefined}
                    target="_blank"
                    rel="noreferrer"
                    className="press flex min-h-[52px] items-center gap-3 px-3 py-2"
                  >
                    <span className="w-[86px] shrink-0 text-[12px] leading-tight text-ink-400">{eventWhen(ev)}</span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[15px] text-ink-100">{ev.title}</span>
                      <span className="block truncate text-[12px] text-ink-500">
                        {ev.calendar}
                        {ev.location && <> · {ev.location}</>}
                      </span>
                    </span>
                  </a>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

type Bucket = 'overdue' | 'today' | 'soon' | 'later' | 'none';
const BUCKETS: { id: Bucket; title: string }[] = [
  { id: 'overdue', title: 'Overdue' },
  { id: 'today', title: 'Today' },
  { id: 'soon', title: 'This week' },
  { id: 'later', title: 'Later' },
  { id: 'none', title: 'Someday' },
];

export default function TasksPanel() {
  const { client, notify } = useHub();
  const open = useHubData((c, s) => c.tasks({ status: 'open' }, s), []);
  const [showDone, setShowDone] = useState(false);
  const done = useHubData((c, s) => (showDone ? c.tasks({ status: 'done' }, s) : Promise.resolve([] as Task[])), [showDone]);
  const [title, setTitle] = useState('');
  const [due, setDue] = useState<string>('');
  const [time, setTime] = useState<string>('');
  const [high, setHigh] = useState(false);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<{ id: number; open: boolean; task: Task } | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const editId = useRef(1);
  const today = isoDay();

  useTopic(['tasks'], () => {
    open.reload();
    if (showDone) done.reload();
  });
  usePullRefresh(() => {
    open.reload();
    if (showDone) done.reload();
  });

  const grouped = useMemo(() => {
    const out: Record<Bucket, Task[]> = { overdue: [], today: [], soon: [], later: [], none: [] };
    const now = new Date();
    for (const t of sortTasks(open.data ?? [])) out[taskDue(t, now).tone].push(t);
    return out;
  }, [open.data, today]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const text = title.trim();
    if (!text || adding) return;
    setAdding(true);
    try {
      const day = due || (time ? today : '');
      const t = await client.createTask({
        title: text,
        ...(day ? { due: day } : {}),
        ...(day && validTime(time) ? { due_time: time } : {}),
        ...(high ? { priority: 'high' as const } : {}),
      });
      haptic(10);
      setFresh(t.id);
      open.setData((ts) => [...(ts ?? []), t]);
      setTitle('');
      setTime('');
      setHigh(false);
      announce('tasks');
      inputRef.current?.focus();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setAdding(false);
    }
  };

  const onChange = (t: Task) => {
    if (t.done && t.next) {
      const next = t.next; // a recurring task: its next occurrence shows up at once
      setFresh(next.id);
      open.setData((ts) => [...(ts ?? []).filter((x) => x.id !== next.id), next]);
    }
    if (t.done) {
      open.setData((ts) => ts?.map((x) => (x.id === t.id ? t : x)));
      window.setTimeout(() => {
        open.setData((ts) => ts?.filter((x) => !(x.id === t.id && x.done)));
        if (showDone) done.setData((ts) => [t, ...(ts ?? []).filter((x) => x.id !== t.id)]);
      }, 900);
    } else {
      const gone = t.removed_next; // reopened: the hub deleted the occurrence its completion had created
      done.setData((ts) => ts?.filter((x) => x.id !== t.id));
      open.setData((ts) => [...(ts ?? []).filter((x) => x.id !== t.id && x.id !== gone), t]);
    }
  };

  const remove = async (t: Task) => {
    try {
      await client.deleteTask(t.id);
      open.setData((ts) => ts?.filter((x) => x.id !== t.id));
      done.setData((ts) => ts?.filter((x) => x.id !== t.id));
      announce('tasks');
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const toggleFromSwipe = async (t: Task) => {
    try {
      onChange(await client.updateTask(t.id, { done: !t.done }));
      announce('tasks');
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const row = (t: Task, i: number) => (
    <div key={t.id} className={fresh === t.id ? 'pop' : 'rise'} style={riseStyle(i)}>
      <SwipeRow
        start={{ icon: 'check', label: t.done ? 'Reopen' : 'Done', tone: 'signal', run: () => void toggleFromSwipe(t) }}
        end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => remove(t) }}
      >
        <TaskRow task={t} onChange={onChange} onOpen={(task) => setEditing({ id: editId.current++, open: true, task })} />
      </SwipeRow>
    </div>
  );

  let n = 0;
  return (
    <div className="space-y-5">
      <form onSubmit={(e) => void add(e)} className="space-y-2 rounded-card bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
        <div className="flex items-center gap-2">
          <input
            ref={inputRef}
            aria-label="New task"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Add a task"
            enterKeyHint="done"
            className="min-h-[46px] min-w-0 flex-1 bg-transparent px-2 text-[16px] text-ink-100 placeholder:text-ink-500 focus:outline-none"
          />
          <button type="submit" disabled={!title.trim() || adding} aria-label="Add task" className="press grid h-11 w-11 shrink-0 place-items-center rounded-full bg-signal text-ink-950 disabled:opacity-30">
            <Icon name="plus" size={22} strokeWidth={2.4} />
          </button>
        </div>
        <div className="no-scrollbar flex gap-2 overflow-x-auto">
          <Chip active={due === today} onClick={() => setDue(due === today ? '' : today)}>
            Today
          </Chip>
          <Chip active={due === addDays(today, 1)} onClick={() => setDue(due === addDays(today, 1) ? '' : addDays(today, 1))}>
            Tomorrow
          </Chip>
          <label className={cx('relative inline-flex min-h-[36px] shrink-0 items-center gap-1.5 rounded-full px-3.5 text-[14px] ring-1 ring-inset', due && due !== today && due !== addDays(today, 1) ? 'bg-ink-100 font-semibold text-ink-950 ring-ink-100' : 'bg-ink-850 text-ink-300 ring-ink-700')}>
            <Icon name="calendar" size={14} />
            {due && due !== today && due !== addDays(today, 1) ? dueInfo(due, today).label : 'Date'}
            <input type="date" aria-label="Due date" value={due} onChange={(e) => setDue(e.target.value)} className="absolute inset-0 opacity-0" />
          </label>
          <label className={cx('relative inline-flex min-h-[36px] shrink-0 items-center gap-1.5 rounded-full px-3.5 text-[14px] ring-1 ring-inset', time ? 'bg-ink-100 font-semibold text-ink-950 ring-ink-100' : 'bg-ink-850 text-ink-300 ring-ink-700')}>
            <Icon name="clock" size={14} />
            {time || 'Time'}
            <input type="time" aria-label="Due time" value={time} onChange={(e) => setTime(e.target.value)} className="absolute inset-0 opacity-0" />
          </label>
          <Chip active={high} onClick={() => setHigh((v) => !v)}>
            <Icon name="flag" size={14} />
            High
          </Chip>
        </div>
      </form>

      {open.error && <ErrorNote message={open.error} onRetry={open.reload} />}
      {open.loading && !open.data && <SkeletonList rows={4} />}
      {open.data && open.data.length === 0 && <EmptyState icon="check" title="All clear" hint="Add a task above, or record a voice note and the tasks are pulled out of it." />}

      {BUCKETS.map(
        (b) =>
          grouped[b.id].length > 0 && (
            <section key={b.id} className="space-y-2">
              <h2 className={cx('px-1 font-display text-[15px] font-semibold', b.id === 'overdue' ? 'text-danger-400' : b.id === 'today' ? 'text-signal' : 'text-ink-400')}>
                {b.title} <span className="font-sans font-normal text-ink-500">{grouped[b.id].length}</span>
              </h2>
              <div className="space-y-2">{grouped[b.id].map((t) => row(t, n++))}</div>
            </section>
          ),
      )}

      <UpcomingEvents />

      <div>
        <button type="button" onClick={() => setShowDone((v) => !v)} className="press flex min-h-[44px] items-center gap-1.5 px-1 text-[14px] text-ink-400">
          <Icon name={showDone ? 'up' : 'down'} size={16} />
          {showDone ? 'Hide completed' : 'Show completed'}
        </button>
        {showDone && (
          <div className="screen-in-up space-y-2">
            {done.loading && !done.data?.length && <SkeletonList rows={2} />}
            {done.data?.map((t, i) => row(t, i))}
            {done.data && done.data.length === 0 && !done.loading && <p className="px-1 text-[14px] text-ink-500">Nothing completed yet.</p>}
          </div>
        )}
      </div>

      {editing && (
        <TaskSheet
          key={editing.id}
          open={editing.open}
          task={editing.task}
          onClose={() => setEditing((e) => (e ? { ...e, open: false } : e))}
          onSaved={(t) => open.setData((ts) => ts?.map((x) => (x.id === t.id ? t : x)))}
          onDeleted={(id) => open.setData((ts) => ts?.filter((x) => x.id !== id))}
        />
      )}
    </div>
  );
}
