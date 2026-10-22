import { useMemo, useRef, useState } from 'react';
import { FileThumb } from '../components/files';
import { Icon } from '../components/icons';
import { SwipeRow } from '../components/SwipeRow';
import { ErrorNote, Meter, SectionTitle, cx, riseStyle } from '../components/ui';
import { useActions } from '../lib/actions';
import { mergeAgenda } from '../lib/agenda';
import { budgetAlerts, currenciesByWeight, currentMonth, formatMoney, isoDay, sortTasks } from '../lib/apps';
import { headlineStats } from '../lib/digest';
import { announce, useTopic } from '../lib/bus';
import { displayName, fileKind } from '../lib/files';
import { errorText, useHub, useHubData } from '../lib/hub';
import { plainText } from '../lib/markdown';
import { usePullRefresh, useNav } from '../lib/nav';
import type { Task } from '../lib/types';
import { MORE_PAGES } from './MoreView';
import PlanCard from './PlanCard';
import { TaskRow } from './notes/TaskRow';

export default function HomeView() {
  const { client, notify } = useHub();
  const { go } = useNav();
  const actions = useActions();
  const today = isoDay();
  const tasks = useHubData((c, s) => c.tasks({ status: 'open' }, s), []);
  const notes = useHubData((c, s) => c.notes({ limit: 6 }, s), []);
  const budget = useHubData((c, s) => c.budget(s), [], 5 * 60_000);
  const spend = useHubData((c, s) => c.expenses(currentMonth(today), s), []);
  const files = useHubData((c, s) => c.driveList('', '', s), []);
  const week = useHubData((c, s) => c.digest('week', false, s), []);
  const calendar = useHubData((c, s) => c.gcalEvents(1, s), [], 5 * 60_000);
  const [leaving, setLeaving] = useState<Set<string>>(new Set());
  const timers = useRef<Record<string, number>>({});

  useTopic(['tasks'], tasks.reload);
  useTopic(['notes'], notes.reload);
  useTopic(['expenses'], spend.reload);
  useTopic(['drive'], files.reload);
  usePullRefresh(() => {
    tasks.reload();
    notes.reload();
    budget.reload();
    spend.reload();
    files.reload();
    week.reload();
    calendar.reload();
  });

  const agenda = useMemo(() => mergeAgenda(calendar.data?.events ?? [], tasks.data ?? [], today), [calendar.data, tasks.data, today]);
  const upNext = useMemo(() => sortTasks(tasks.data ?? []).slice(0, 6), [tasks.data]);
  const alerts = budgetAlerts(budget.data?.items ?? []);
  const recentFiles = useMemo(
    () => [...(files.data?.objects ?? [])].sort((a, b) => (b.last_modified ?? '').localeCompare(a.last_modified ?? '')).slice(0, 10),
    [files.data],
  );
  const totals = spend.data?.summary.total_by_currency ?? {};
  const mainCurrency = currenciesByWeight(totals)[0];

  // A completed task stays for a beat (so the check animation is seen), then leaves the list.
  const onTask = (t: Task) => {
    tasks.setData((ts) => ts?.map((x) => (x.id === t.id ? t : x)));
    window.clearTimeout(timers.current[t.id]);
    if (t.done) {
      timers.current[t.id] = window.setTimeout(() => {
        setLeaving((s) => new Set(s).add(t.id));
        window.setTimeout(() => {
          tasks.setData((ts) => ts?.filter((x) => x.id !== t.id));
          setLeaving((s) => {
            const n = new Set(s);
            n.delete(t.id);
            return n;
          });
        }, 240);
      }, 700);
    }
  };

  const removeTask = async (t: Task) => {
    try {
      await client.deleteTask(t.id);
      announce('tasks');
      tasks.setData((ts) => ts?.filter((x) => x.id !== t.id));
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  return (
    <div className="space-y-7">
      {alerts.length > 0 && (
        <button
          type="button"
          onClick={() => go('more', 'budget')}
          className={cx(
            'press rise block w-full rounded-card p-4 text-left ring-1 ring-inset',
            alerts[0].status === 'over' ? 'bg-danger-400/10 ring-danger-400/30' : 'bg-amber-400/10 ring-amber-400/30',
          )}
        >
          <span className={cx('flex items-center gap-2 font-display text-[16px] font-semibold', alerts[0].status === 'over' ? 'text-danger-400' : 'text-amber-400')}>
            <Icon name="gauge" size={18} />
            {alerts.length} free-tier limit{alerts.length === 1 ? '' : 's'} {alerts.some((a) => a.status === 'over') ? 'reached or close' : 'getting close'}
          </span>
          <span className="mt-3 block space-y-2.5">
            {alerts.slice(0, 3).map((a) => (
              <span key={`${a.provider}-${a.metric}-${a.window}`} className="block">
                <span className="flex justify-between gap-2 text-[14px]">
                  <span className="truncate text-ink-100">
                    {a.provider} <span className="text-ink-400">{a.metric}{a.window ? ` per ${a.window}` : ''}</span>
                  </span>
                  <span className="shrink-0 text-ink-200">{a.fraction !== null ? `${Math.round(a.fraction * 100)}%` : ''}</span>
                </span>
                <Meter value={a.fraction} tone={a.status === 'over' ? 'bad' : 'warn'} className="mt-1 h-1.5" />
              </span>
            ))}
          </span>
        </button>
      )}

      <button
        type="button"
        onClick={() => go('more', 'summary')}
        className="press rise block w-full rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800"
        style={riseStyle(4)}
        aria-label="This week: open the summary"
      >
        <span className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 font-display text-[17px] font-semibold text-ink-100">
            <span className="grid h-8 w-8 place-items-center rounded-full bg-signal/15 text-signal">
              <Icon name="chart" size={16} />
            </span>
            This week
          </span>
          <span className="flex items-center gap-0.5 text-[14px] text-signal">
            Summary
            <Icon name="right" size={16} />
          </span>
        </span>
        <span className="mt-3 grid grid-cols-3 gap-2">
          {week.data
            ? headlineStats(week.data).map((st) => (
                <span key={st.label} className="block min-w-0">
                  <span className="block truncate font-display text-[19px] font-semibold leading-tight text-ink-100">{st.value}</span>
                  <span className="block truncate text-[12px] text-ink-400">{st.label}</span>
                </span>
              ))
            : week.error
              ? <span className="col-span-3 block text-[14px] text-ink-400">Spending, tasks and notes of the last 7 days.</span>
              : [0, 1, 2].map((i) => (
                <span key={i} className="block space-y-1.5" aria-hidden="true">
                  <span className="skeleton block h-5 w-3/4" />
                  <span className="skeleton block h-3 w-1/2" />
                </span>
              ))}
        </span>
        {week.data?.highlights[0] && <span className="mt-3 line-clamp-2 block text-[14px] leading-snug text-ink-300">{week.data.highlights[0]}</span>}
      </button>

      {agenda.length > 0 && (
        <section aria-label="Today's agenda">
          <SectionTitle
            action={
              <button type="button" onClick={() => go('notes', 'tasks')} className="press min-h-[44px] px-2 text-[14px] text-signal">
                Agenda
              </button>
            }
          >
            Today
          </SectionTitle>
          <div className="no-scrollbar -mx-4 flex snap-x gap-2 overflow-x-auto px-4 pb-1">
            {agenda.map((item, i) => {
              const isEvent = item.kind === 'event';
              const body = (
                <>
                  <span className={cx('flex items-center gap-1.5 text-[12px] font-semibold', isEvent ? 'text-signal' : 'text-mint-400')}>
                    <Icon name={isEvent ? 'calendar' : 'check'} size={13} />
                    {item.label}
                    {item.end && <span className="font-normal text-ink-500">–{item.end}</span>}
                  </span>
                  <span className="mt-1 line-clamp-2 text-[14px] leading-snug text-ink-100">{item.title}</span>
                  <span className="mt-auto block truncate pt-1 text-[11px] text-ink-500">{isEvent ? (item.event?.calendar ?? 'Calendar') : 'Task'}</span>
                </>
              );
              const cls = 'press rise flex h-[104px] w-[42%] min-w-[140px] max-w-[200px] shrink-0 snap-start flex-col rounded-card bg-ink-900 p-3 text-left ring-1 ring-inset ring-ink-800';
              return isEvent && item.event?.link ? (
                <a key={item.key} href={item.event.link} target="_blank" rel="noreferrer" className={cls} style={riseStyle(i)} aria-label={`${item.label} ${item.title} (event)`}>
                  {body}
                </a>
              ) : (
                <button key={item.key} type="button" onClick={() => go('notes', 'tasks')} className={cls} style={riseStyle(i)} aria-label={`${item.label} ${item.title} (${isEvent ? 'event' : 'task'})`}>
                  {body}
                </button>
              );
            })}
          </div>
        </section>
      )}

      <PlanCard />

      <section>
        <SectionTitle
          action={
            <button type="button" onClick={() => go('notes', 'tasks')} className="press min-h-[44px] px-2 text-[14px] text-signal">
              All tasks
            </button>
          }
        >
          Up next
        </SectionTitle>
        {tasks.error && <ErrorNote message={tasks.error} onRetry={tasks.reload} />}
        {tasks.loading && !tasks.data && (
          <div className="space-y-2" aria-hidden="true">
            {[0, 1, 2].map((i) => (
              <div key={i} className="flex items-center gap-3 rounded-row bg-ink-900 px-3 py-4">
                <div className="skeleton h-6 w-6 rounded-full" />
                <div className="skeleton h-3.5 flex-1" style={{ maxWidth: `${60 - i * 12}%` }} />
              </div>
            ))}
          </div>
        )}
        {tasks.data && upNext.length === 0 && (
          <button type="button" onClick={() => actions.recordVoice()} className="press w-full rounded-card bg-ink-900 p-5 text-left ring-1 ring-inset ring-ink-800">
            <span className="block font-display text-[17px] font-semibold text-ink-100">Nothing due. Nice.</span>
            <span className="block text-[14px] text-ink-400">Say what is on your mind and the tasks get pulled out for you.</span>
          </button>
        )}
        <div className="space-y-2">
          {upNext.map((t, i) => (
            <div
              key={t.id}
              style={riseStyle(i)}
              className={cx('rise transition-[opacity,transform] duration-base ease-out', leaving.has(t.id) && 'translate-x-6 opacity-0')}
            >
              <SwipeRow
                start={{ icon: 'check', label: t.done ? 'Reopen' : 'Done', tone: 'signal', run: () => void client.updateTask(t.id, { done: !t.done }).then(onTask, (e) => notify(errorText(e), 'error')) }}
                end={{ icon: 'trash', label: 'Delete', tone: 'danger', run: () => removeTask(t) }}
              >
                <TaskRow task={t} onChange={onTask} />
              </SwipeRow>
            </div>
          ))}
        </div>
      </section>

      <section>
        <SectionTitle
          action={
            <button type="button" onClick={() => go('notes')} className="press min-h-[44px] px-2 text-[14px] text-signal">
              All notes
            </button>
          }
        >
          Recent notes
        </SectionTitle>
        <div className="no-scrollbar -mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto px-4 pb-1">
          {notes.loading && !notes.data && [0, 1].map((i) => <div key={i} className="skeleton h-[136px] w-[72%] shrink-0 rounded-card" />)}
          {notes.data?.notes.length === 0 && (
            <button type="button" onClick={() => actions.newNote()} className="press h-[136px] w-full rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800">
              <span className="block font-display text-[17px] font-semibold text-ink-100">Write your first note</span>
              <span className="block text-[14px] text-ink-400">Ideas, lists, anything. It saves as you type.</span>
            </button>
          )}
          {notes.data?.notes.map((n, i) => (
            <button
              key={n.id}
              type="button"
              onClick={() => actions.openNote(n)}
              style={riseStyle(i)}
              className="press rise flex h-[136px] w-[72%] max-w-[300px] shrink-0 snap-start flex-col rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800"
            >
              <span className="flex items-center gap-1.5 text-[12px] text-ink-500">
                {n.pinned && <Icon name="pin" size={13} className="text-signal" />}
                {n.source === 'voice' && <Icon name="mic" size={13} className="text-danger-400" />}
                {n.notebook}
              </span>
              <span className="mt-1 line-clamp-1 font-display text-[17px] font-semibold text-ink-100">{n.title || 'Untitled'}</span>
              <span className="mt-1 line-clamp-3 text-[14px] leading-snug text-ink-400">{plainText(n.body, 140)}</span>
            </button>
          ))}
        </div>
      </section>

      {recentFiles.length > 0 && (
        <section>
          <SectionTitle
            action={
              <button type="button" onClick={() => go('drive')} className="press min-h-[44px] px-2 text-[14px] text-signal">
                Open Drive
              </button>
            }
          >
            Latest files
          </SectionTitle>
          <div className="no-scrollbar -mx-4 flex gap-3 overflow-x-auto px-4">
            {recentFiles.map((o) => (
              <button key={`${o.backend}/${o.key}`} type="button" onClick={() => actions.preview(o, recentFiles)} className="press w-[92px] shrink-0 text-left">
                <FileThumb obj={o} size={92} className="rounded-[18px]" />
                <span className="mt-1.5 block truncate text-[13px] text-ink-200">{displayName(o.key)}</span>
                <span className="block text-[11px] text-ink-500">{fileKind(o.key, o.content_type)}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      <button type="button" onClick={() => go('more', 'expenses')} className="press flex w-full items-center gap-4 rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800">
        <span className="grid h-12 w-12 shrink-0 place-items-center rounded-full bg-mint-400/15 text-mint-400">
          <Icon name="wallet" size={22} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] text-ink-400">Spent this month</span>
          <span className="block font-display text-[24px] font-semibold leading-tight text-ink-100">
            {mainCurrency ? formatMoney(totals[mainCurrency], mainCurrency) : spend.loading ? '…' : 'Nothing yet'}
          </span>
          {Object.keys(totals).length > 1 && (
            <span className="block truncate text-[13px] text-ink-400">
              plus{' '}
              {currenciesByWeight(totals)
                .slice(1)
                .map((c) => formatMoney(totals[c], c))
                .join(', ')}
            </span>
          )}
        </span>
        <Icon name="right" size={18} className="text-ink-500" />
      </button>

      <section>
        <SectionTitle>Everything else</SectionTitle>
        <div className="grid grid-cols-4 gap-y-4">
          {MORE_PAGES.map((p) => (
            <button key={p.id} type="button" onClick={() => go('more', p.id)} className="press flex flex-col items-center gap-1.5 text-[12px] text-ink-300">
              <span className="grid h-12 w-12 place-items-center rounded-[16px] bg-ink-850 text-ink-200 ring-1 ring-inset ring-ink-700">
                <Icon name={p.icon} size={21} />
              </span>
              <span className="truncate">{p.short}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
