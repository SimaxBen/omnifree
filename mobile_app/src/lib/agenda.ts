// Pure helpers for the Google Calendar agenda (Home "Today" strip, Tasks "Upcoming events"). No React, no I/O.
// Days and times are Africa/Casablanca, like the hub.

import { HOME_TZ, addDays, clockTime, isoDay } from './apps';
import type { GCalEvent, Task } from './types';

export interface AgendaItem {
  key: string;
  kind: 'event' | 'task';
  /** HH:MM home time; null for all-day events (and events that started on an earlier day). */
  time: string | null;
  /** End HH:MM of a timed event on the same day. */
  end: string | null;
  title: string;
  /** "All day", "Continues" or the start time. */
  label: string;
  event?: GCalEvent;
  task?: Task;
}

/** Local day (YYYY-MM-DD) and time (HH:MM) of an ISO stamp; all-day dates pass through. */
function local(stamp: string, tz = HOME_TZ): { day: string; time: string } {
  if (/^\d{4}-\d{2}-\d{2}$/.test(stamp)) return { day: stamp, time: '00:00' };
  const d = new Date(stamp);
  if (Number.isNaN(d.getTime())) return { day: stamp.slice(0, 10), time: stamp.slice(11, 16) || '00:00' };
  return { day: isoDay(d, tz), time: clockTime(d, tz) };
}

/** Does `ev` take place (at least partly) on `day`? All-day end dates are exclusive. */
export function eventOnDay(ev: GCalEvent, day: string, tz = HOME_TZ): boolean {
  if (ev.all_day) return ev.start <= day && (ev.end > day || (ev.end === ev.start && ev.start === day));
  const s = local(ev.start, tz);
  const e = local(ev.end || ev.start, tz);
  if (s.day === day) return true;
  return s.day < day && (e.day > day || (e.day === day && e.time > '00:00'));
}

/** Today's agenda: events on `day` merged with the open timed tasks due that day, in time order
 * (all-day events first, then by time; at the same time events before tasks). */
export function mergeAgenda(events: GCalEvent[], tasks: Task[], day: string, tz = HOME_TZ): AgendaItem[] {
  const items: (AgendaItem & { rank: number })[] = [];
  for (const ev of events) {
    if (!eventOnDay(ev, day, tz)) continue;
    if (ev.all_day) {
      items.push({ key: `e:${ev.calendar_id}:${ev.id}`, kind: 'event', time: null, end: null, title: ev.title, label: 'All day', event: ev, rank: 0 });
      continue;
    }
    const s = local(ev.start, tz);
    const e = local(ev.end || ev.start, tz);
    const started = s.day === day;
    items.push({
      key: `e:${ev.calendar_id}:${ev.id}`,
      kind: 'event',
      time: started ? s.time : null,
      end: e.day === day && (!started || e.time !== s.time) ? e.time : null,
      title: ev.title,
      label: started ? s.time : 'Continues',
      event: ev,
      rank: started ? 1 : 0,
    });
  }
  for (const t of tasks) {
    if (t.done || t.due !== day || !t.due_time || !/^\d{2}:\d{2}$/.test(t.due_time)) continue;
    items.push({ key: `t:${t.id}`, kind: 'task', time: t.due_time, end: null, title: t.title, label: t.due_time, task: t, rank: 1 });
  }
  return items
    .sort((a, b) => a.rank - b.rank || (a.time ?? '').localeCompare(b.time ?? '') || (a.kind === b.kind ? 0 : a.kind === 'event' ? -1 : 1) || a.title.localeCompare(b.title))
    .map(({ rank: _rank, ...item }) => item);
}

export interface EventDay {
  day: string;
  label: string;
  events: GCalEvent[];
}

function dayLabel(day: string, today: string): string {
  if (today && day === today) return 'Today';
  if (today && day === addDays(today, 1)) return 'Tomorrow';
  const [y, m, d] = day.split('-').map(Number);
  return new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' }).format(new Date(Date.UTC(y, m - 1, d)));
}

/** Events grouped by the day they start (an all-day event spanning several days shows on each day from today on). */
export function groupEventsByDay(events: GCalEvent[], today: string, days = 7, tz = HOME_TZ): EventDay[] {
  const out: EventDay[] = [];
  for (let i = 0; i < days; i++) {
    const day = addDays(today, i);
    const list = events.filter((ev) => (ev.all_day ? eventOnDay(ev, day, tz) : local(ev.start, tz).day === day || (i === 0 && eventOnDay(ev, day, tz))));
    if (list.length) out.push({ day, label: dayLabel(day, today), events: list });
  }
  return out;
}

/** "09:00–09:30", "All day" or "Mar 14 – 16" for a multi-day all-day event. */
export function eventWhen(ev: GCalEvent, tz = HOME_TZ): string {
  if (ev.all_day) {
    const last = addDays(ev.end || ev.start, -1);
    return last > ev.start ? `All day, until ${dayLabel(last, '')}` : 'All day';
  }
  const s = local(ev.start, tz);
  const e = local(ev.end || ev.start, tz);
  if (e.day !== s.day) return `${s.time} → ${dayLabel(e.day, '')} ${e.time}`;
  return e.time !== s.time ? `${s.time}–${e.time}` : s.time;
}
