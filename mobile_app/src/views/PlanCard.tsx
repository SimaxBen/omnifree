import { useEffect, useState } from 'react';
import { Icon } from '../components/icons';
import { useConfirm } from '../components/Sheet';
import { Badge, Button, ErrorNote, Toggle, cx, haptic, riseStyle } from '../components/ui';
import { clockTime } from '../lib/apps';
import { announce, useTopic } from '../lib/bus';
import { errorText, useHub, useHubData } from '../lib/hub';
import { useNav } from '../lib/nav';
import { acceptPlan, canBook, clearPlan, durationLabel, getPlan, makePlan, planStats, visibleWindow, type DayPlan, type PlanBlock } from '../lib/plan';

const COLLAPSED = 6;

const KIND_STYLE: Record<PlanBlock['kind'], { dot: string; text: string; icon: 'calendar' | 'check' | 'clock' }> = {
  event: { dot: 'bg-signal', text: 'text-signal', icon: 'calendar' },
  task: { dot: 'bg-mint-400', text: 'text-mint-400', icon: 'check' },
  break: { dot: 'bg-ink-500', text: 'text-ink-400', icon: 'clock' },
};

/** `current` is the live block's index in `blocks` (computed on the whole day, not on the shown slice). */
function Timeline({ blocks, current, onTask }: { blocks: PlanBlock[]; current: number; onTask: () => void }) {
  return (
    <ol className="relative mt-3 space-y-1" aria-label="Today's plan">
      <span aria-hidden="true" className="absolute bottom-3 left-[61px] top-3 w-px bg-ink-800" />
      {blocks.map((b, i) => {
        const k = KIND_STYLE[b.kind];
        const live = i === current;
        const row = (
          <>
            <span className="w-[46px] shrink-0 pt-0.5 text-right font-display text-[13px] font-semibold tabular-nums text-ink-300">{b.start}</span>
            <span className={cx('relative z-[1] mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ring-4 ring-ink-900', k.dot, live && 'animate-pulse')} />
            <span className="min-w-0 flex-1">
              <span className={cx('flex items-center gap-1.5 text-[15px] leading-snug', b.kind === 'break' || b.done ? 'text-ink-400' : 'text-ink-100')}>
                <span className={cx('truncate', b.done && 'line-through decoration-ink-500')}>{b.title}</span>
                {b.done && <Icon name="check" size={12} className="shrink-0 text-mint-400" aria-label="Done" />}
                {b.fixed && b.kind === 'task' && <Icon name="pin" size={12} className="shrink-0 text-ink-500" aria-label="Fixed time" />}
              </span>
              <span className="block truncate text-[12px] text-ink-500">
                <span className={k.text}>{durationLabel(b)}</span>
                {b.reason ? ` · ${b.reason}` : ''}
              </span>
            </span>
          </>
        );
        const cls = cx('flex w-full items-start gap-3 rounded-row px-1 py-1.5 text-left', live && 'bg-ink-850');
        return (
          <li key={`${b.start}-${b.kind}-${b.ref_id ?? i}`} className="rise" style={riseStyle(i)} aria-current={live ? 'step' : undefined}>
            {b.kind === 'task' ? (
              <button type="button" onClick={onTask} className={cx('press', cls)}>
                {row}
              </button>
            ) : (
              <div className={cls}>{row}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** Home: "Plan my day": the hub orders today's tasks between the calendar events; Accept can book them. */
export default function PlanCard() {
  const { client, notify } = useHub();
  const { go } = useNav();
  const confirm = useConfirm();
  const plan = useHubData((c, s) => getPlan(c, s), []);
  const [busy, setBusy] = useState<'plan' | 'accept' | 'clear' | null>(null);
  const [book, setBook] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [now, setNow] = useState(() => clockTime());

  // A ticked task shows as done in the timeline (the hub flags it), and a new one can make "Plan my day" useful.
  useTopic(['tasks'], () => plan.reload());
  useEffect(() => {
    const t = window.setInterval(() => setNow(clockTime()), 60_000);
    return () => window.clearInterval(t);
  }, []);

  const run = async (kind: 'plan' | 'accept' | 'clear', fn: () => Promise<DayPlan | null>, done?: (p: DayPlan | null) => void) => {
    haptic(8);
    setBusy(kind);
    try {
      const res = await fn();
      if (res) plan.setData(() => res);
      done?.(res);
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const doPlan = async (p?: DayPlan | null) => {
    // Re-planning a booked plan removes its Google Calendar blocks (the hub clears them first).
    if (p?.status === 'accepted' && p.booked > 0) {
      const ok = await confirm({
        title: 'Re-plan today?',
        body: `Re-planning removes the ${p.booked} block${p.booked === 1 ? '' : 's'} from Google Calendar. Accept the new plan to book again.`,
        confirmLabel: 'Re-plan',
        tone: 'danger',
      });
      if (!ok) return;
    }
    await run('plan', () => makePlan(client), (res) => {
      setExpanded(false);
      if (res?.cleared_previous === false) notify('Old calendar blocks not all removed: check Google Calendar', 'error');
      else if (res?.cleared_previous) notify('Calendar blocks removed: accept to book again', 'ok');
    });
  };
  const doAccept = (p: DayPlan) =>
    run('accept', () => acceptPlan(client, book && canBook(p)), (res) => {
      if (!res) return;
      if (res.booked) {
        notify(`Plan accepted: ${res.booked} block${res.booked === 1 ? '' : 's'} in Google Calendar`, 'ok');
        announce('tasks');
      } else notify('Plan accepted', 'ok');
      if (res.errors?.length) notify(res.errors[0], 'error');
    });
  const doClear = async (p: DayPlan) => {
    const ok = await confirm({
      title: 'Clear today’s plan?',
      body: p.booked ? `The ${p.booked} block${p.booked === 1 ? '' : 's'} added to Google Calendar are removed too. Your tasks stay.` : 'Your tasks stay as they are.',
      confirmLabel: 'Clear plan',
      tone: 'danger',
    });
    if (!ok) return;
    await run('clear', async () => {
      const res = await clearPlan(client);
      if (res.error) notify(`Calendar blocks not all removed: ${res.error}`, 'error');
      return getPlan(client);
    });
  };

  const p = plan.data;
  const stats = p ? planStats(p) : null;
  const blocks = p?.blocks ?? [];
  const win = visibleWindow(blocks, now, expanded ? blocks.length : COLLAPSED);
  const shown = blocks.slice(win.start, win.end);

  return (
    <section aria-label="Plan my day" className="rise rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800" style={riseStyle(3)}>
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2 font-display text-[17px] font-semibold text-ink-100">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-signal/15 text-signal">
            <Icon name="spark" size={16} />
          </span>
          <span className="truncate">Plan my day</span>
        </span>
        {p && p.status !== 'none' && (
          <span className="flex shrink-0 items-center gap-1.5">
            {p.status === 'accepted' && <Badge tone="ok">{p.booked ? `In calendar · ${p.booked}` : 'Accepted'}</Badge>}
            <Badge tone={p.source === 'llm' ? 'signal' : 'neutral'}>{p.source === 'llm' ? 'AI' : 'Quick plan'}</Badge>
          </span>
        )}
      </div>

      {plan.error && !p && <div className="mt-3"><ErrorNote message={plan.error} onRetry={plan.reload} /></div>}
      {plan.loading && !p && (
        <div className="mt-3 space-y-2" aria-hidden="true">
          <div className="skeleton h-3.5 w-3/4" />
          <div className="skeleton h-3.5 w-1/2" />
        </div>
      )}

      {busy === 'plan' && (
        <div className="mt-4 flex items-center gap-3 text-[14px] text-ink-300" aria-live="polite">
          <span className="dot-wave font-display text-[24px] leading-none text-signal">
            <span>.</span>
            <span>.</span>
            <span>.</span>
          </span>
          Fitting your tasks between today’s events…
        </div>
      )}

      {p && p.status === 'none' && busy !== 'plan' && (
        <div className="mt-3 space-y-3">
          <p className="text-[14px] leading-snug text-ink-400">
            Overdue, due-today and high-priority tasks, placed in the free time between your calendar events (08:00–21:00), with breaks.
          </p>
          <Button variant="primary" icon="spark" className="w-full" onClick={() => void doPlan(p)} disabled={busy !== null}>
            Plan my day
          </Button>
        </div>
      )}

      {p && p.status !== 'none' && busy !== 'plan' && (
        <>
          {stats && (
            <p className="mt-2 text-[13px] text-ink-400">
              {stats.tasks} task{stats.tasks === 1 ? '' : 's'}
              {stats.done ? ` (${stats.done} done)` : ''} · {Math.round((stats.focusMinutes / 60) * 10) / 10} h of focus
              {stats.events ? ` · ${stats.events} event${stats.events === 1 ? '' : 's'}` : ''}
              {p.window ? ` · from ${p.window.start}` : ''}
            </p>
          )}
          {p.agenda_error && (
            <p className="mt-2 flex items-start gap-1.5 text-[13px] leading-snug text-amber-400" role="status">
              <Icon name="calendar" size={14} className="mt-0.5 shrink-0" />
              Couldn’t read Google Calendar: events were not considered. Re-plan to try again.
            </p>
          )}
          {win.start > 0 && (
            <p className="mt-2 px-1 text-[12px] text-ink-500">
              {win.start} earlier block{win.start === 1 ? '' : 's'}
            </p>
          )}
          {blocks.length ? (
            <Timeline blocks={shown} current={win.current - win.start} onTask={() => go('notes', 'tasks')} />
          ) : (
            <p className="mt-3 text-[14px] text-ink-400">Nothing to place today{p.window && p.window.start >= p.window.end ? ': the day is over' : ''}.</p>
          )}
          {blocks.length > COLLAPSED && (
            <button type="button" onClick={() => setExpanded((v) => !v)} className="press mt-1 flex min-h-[40px] items-center gap-1 px-1 text-[14px] text-signal">
              <Icon name={expanded ? 'up' : 'down'} size={16} />
              {expanded ? 'Show less' : `Show all ${blocks.length}`}
            </button>
          )}
          {p.unscheduled.length > 0 && (
            <p className="mt-2 px-1 text-[13px] leading-snug text-ink-500">
              Not today: {p.unscheduled.slice(0, 3).map((u) => u.title).join(', ')}
              {p.unscheduled.length > 3 ? ` and ${p.unscheduled.length - 3} more` : ''}
            </p>
          )}

          {p.status === 'proposed' && canBook(p) && (
            <label className="mt-3 flex min-h-[44px] items-center justify-between gap-3 rounded-row bg-ink-850 px-3">
              <span className="flex items-center gap-2 text-[14px] text-ink-200">
                <Icon name="calendar" size={16} className="text-signal" />
                Book task blocks in Google Calendar
              </span>
              <Toggle checked={book} onChange={setBook} label="Book task blocks in Google Calendar" />
            </label>
          )}

          <div className="mt-3 flex gap-2">
            {p.status === 'proposed' ? (
              <Button variant="primary" icon="check" className="flex-1" onClick={() => void doAccept(p)} disabled={busy !== null || !blocks.length}>
                {busy === 'accept' ? 'Accepting…' : 'Accept'}
              </Button>
            ) : (
              <Button variant="danger" icon="trash" className="flex-1" onClick={() => void doClear(p)} disabled={busy !== null}>
                {busy === 'clear' ? 'Clearing…' : 'Clear plan'}
              </Button>
            )}
            <Button icon="refresh" onClick={() => void doPlan(p)} disabled={busy !== null} aria-label="Plan again">
              Re-plan
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
