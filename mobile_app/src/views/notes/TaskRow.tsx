import { useState } from 'react';
import { Icon } from '../../components/icons';
import { PendingBadge } from '../../components/OfflineBanner';
import { cx, haptic } from '../../components/ui';
import { clockTime, isoDay, taskDue } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { errorText, useHub } from '../../lib/hub';
import { activeBlock, checklistProgress, recurrenceLabel, slotLabel } from '../../lib/tasks';
import type { Task } from '../../lib/types';

const DUE_TONE = {
  overdue: 'text-danger-400',
  today: 'text-signal',
  soon: 'text-ink-200',
  later: 'text-ink-400',
  none: 'text-ink-500',
} as const;

/** The check circle: fills saffron with a spring, draws the tick, and throws a ring outwards. */
export function CheckButton({ done, onToggle, label }: { done: boolean; onToggle: () => void; label: string }) {
  const [burst, setBurst] = useState(0);
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={done}
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        if (!done) {
          setBurst((b) => b + 1);
          haptic([8, 30, 14]);
        } else haptic(6);
        onToggle();
      }}
      className="press relative grid h-11 w-11 shrink-0 place-items-center"
    >
      {burst > 0 && done && <span key={burst} aria-hidden="true" className="ring-burst absolute h-6 w-6 rounded-full ring-2 ring-signal" />}
      <span className={cx('grid h-6 w-6 place-items-center rounded-full ring-2 ring-inset transition-colors duration-base', done ? 'ring-signal' : 'ring-ink-500')}>
        {done && (
          <span className="check-fill grid h-6 w-6 place-items-center rounded-full bg-signal text-ink-950">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path className="check-draw" d="M5 12.5l4.5 4.5L19 7.5" />
            </svg>
          </span>
        )}
      </span>
    </button>
  );
}

export function TaskRow({ task, onChange, onOpen, compact }: { task: Task; onChange: (t: Task) => void; onOpen?: (t: Task) => void; compact?: boolean }) {
  const { client, notify } = useHub();
  const due = taskDue(task);
  const repeat = task.recurrence ? task.recurrence_label || recurrenceLabel(task.recurrence) : '';
  const progress = checklistProgress(task.checklist);
  const today = isoDay();
  const block = activeBlock(task, `${today}T${clockTime()}`);

  const toggle = async () => {
    const next = { ...task, done: !task.done, done_at: task.done ? null : new Date().toISOString() };
    onChange(next);
    try {
      onChange(await client.updateTask(task.id, { done: next.done }));
      announce('tasks');
    } catch (err) {
      onChange(task);
      notify(errorText(err), 'error');
    }
  };

  return (
    <div className={cx('flex items-center gap-1 pr-3', compact ? 'min-h-[52px]' : 'min-h-[60px]')}>
      <CheckButton done={task.done} onToggle={() => void toggle()} label={`${task.done ? 'Reopen' : 'Complete'} ${task.title}`} />
      <button type="button" onClick={() => onOpen?.(task)} disabled={!onOpen} className="min-w-0 flex-1 py-2 text-left">
        <span className="flex min-w-0 items-center gap-1.5">
          <span data-done={task.done} className={cx('strike block w-fit min-w-0 max-w-full truncate text-[15px]', task.done ? 'text-ink-500' : 'text-ink-100')}>
            {task.title}
          </span>
          <PendingBadge item={task} />
        </span>
        {(task.due || task.priority !== 'normal' || task.details || progress.total > 0 || block) && !task.done && (
          <span className="flex items-center gap-2 text-[13px]">
            {task.due && (
              <span className={cx('inline-flex shrink-0 items-center gap-1', DUE_TONE[due.tone])}>
                <Icon name="calendar" size={13} />
                {due.label}
              </span>
            )}
            {repeat && (
              <span className="inline-flex shrink-0 items-center text-ink-400" title={repeat} aria-label={`Repeats: ${repeat}`}>
                <Icon name="repeat" size={13} />
              </span>
            )}
            {progress.total > 0 && (
              <span className={cx('inline-flex shrink-0 items-center gap-1 tabular-nums', progress.done === progress.total ? 'text-mint-400' : 'text-ink-400')} aria-label={`Checklist ${progress.label}`}>
                <Icon name="check" size={13} />
                {progress.label}
              </span>
            )}
            {block && (
              <span className="inline-flex shrink-0 items-center gap-1 text-signal" aria-label={`Time blocked ${slotLabel(block, today)}`}>
                <Icon name="clock" size={13} />
                {slotLabel({ start: block.start, end: null }, today)}
              </span>
            )}
            {task.priority === 'high' && (
              <span className="inline-flex items-center gap-1 text-amber-400">
                <Icon name="flag" size={13} />
                High
              </span>
            )}
            {task.priority === 'low' && <span className="text-ink-500">Low</span>}
            {task.details && <span className="truncate text-ink-500">{task.details}</span>}
          </span>
        )}
      </button>
    </div>
  );
}
