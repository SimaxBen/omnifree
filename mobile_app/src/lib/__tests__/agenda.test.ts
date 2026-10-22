import { describe, expect, it } from 'vitest';
import { eventOnDay, eventWhen, groupEventsByDay, mergeAgenda } from '../agenda';
import type { GCalEvent, Task } from '../types';

const DAY = '2030-03-12';

function ev(id: string, title: string, start: string, end: string, extra: Partial<GCalEvent> = {}): GCalEvent {
  return { id, calendar: 'Owner', calendar_id: 'owner@gmail.com', title, start, end, all_day: !start.includes('T'), location: null, link: null, ...extra };
}

function task(id: string, title: string, due: string | null, due_time: string | null, done = false): Task {
  return { id, title, details: null, due, due_time, done, priority: 'normal', note_id: null, created_at: '2030-03-01T00:00:00Z', done_at: null };
}

describe('mergeAgenda', () => {
  it('merges events with the timed tasks of the day, all-day first, then by time', () => {
    const events = [
      ev('a', 'Standup', '2030-03-12T09:00:00+01:00', '2030-03-12T09:15:00+01:00'),
      ev('b', 'Birthday', '2030-03-12', '2030-03-13'),
      ev('c', 'Tomorrow', '2030-03-13T09:00:00+01:00', '2030-03-13T10:00:00+01:00'),
      ev('d', 'Lunch', '2030-03-12T12:00:00+01:00', '2030-03-12T13:00:00+01:00'),
      ev('e', 'Same time', '2030-03-12T15:00:00+01:00', '2030-03-12T15:30:00+01:00'),
    ];
    const tasks = [
      task('1', 'Call Karim', DAY, '15:00'),
      task('2', 'No time', DAY, null),
      task('3', 'Done', DAY, '10:00', true),
      task('4', 'Other day', '2030-03-13', '08:00'),
      task('5', 'Early', DAY, '08:30'),
    ];
    const items = mergeAgenda(events, tasks, DAY);
    expect(items.map((i) => [i.kind, i.label, i.title])).toEqual([
      ['event', 'All day', 'Birthday'],
      ['task', '08:30', 'Early'],
      ['event', '09:00', 'Standup'],
      ['event', '12:00', 'Lunch'],
      ['event', '15:00', 'Same time'],
      ['task', '15:00', 'Call Karim'],
    ]);
    expect(items[2].end).toBe('09:15');
    expect(items[1].task?.id).toBe('5');
    expect(new Set(items.map((i) => i.key)).size).toBe(items.length);
  });

  it('converts other offsets to Morocco time and keeps events that started the day before', () => {
    const items = mergeAgenda(
      [
        ev('utc', 'UTC meeting', '2030-03-12T08:00:00Z', '2030-03-12T09:00:00Z'),
        ev('night', 'Night shift', '2030-03-11T22:00:00+01:00', '2030-03-12T06:00:00+01:00'),
        ev('ended', 'Ended at midnight', '2030-03-11T22:00:00+01:00', '2030-03-12T00:00:00+01:00'),
      ],
      [],
      DAY,
    );
    expect(items.map((i) => [i.label, i.title, i.end])).toEqual([
      ['Continues', 'Night shift', '06:00'],
      [new Intl.DateTimeFormat('en-GB', { timeZone: 'Africa/Casablanca', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date('2030-03-12T08:00:00Z')), 'UTC meeting', expect.any(String)],
    ]);
  });

  it('is empty without anything on the day', () => {
    expect(mergeAgenda([], [task('1', 'x', null, null)], DAY)).toEqual([]);
  });
});

describe('eventOnDay / groupEventsByDay / eventWhen', () => {
  it('treats all-day end dates as exclusive', () => {
    const trip = ev('t', 'Trip', '2030-03-12', '2030-03-15');
    expect(['2030-03-11', '2030-03-12', '2030-03-14', '2030-03-15'].map((d) => eventOnDay(trip, d))).toEqual([false, true, true, false]);
    expect(eventWhen(trip)).toBe('All day, until Thu 14 Mar');
    expect(eventWhen(ev('b', 'B', DAY, '2030-03-13'))).toBe('All day');
  });

  it('groups by day with Today / Tomorrow labels', () => {
    const groups = groupEventsByDay(
      [
        ev('a', 'Standup', '2030-03-12T09:00:00+01:00', '2030-03-12T09:15:00+01:00'),
        ev('b', 'Dentist', '2030-03-13T10:00:00+01:00', '2030-03-13T10:30:00+01:00'),
        ev('c', 'Trip', '2030-03-14', '2030-03-16'),
      ],
      DAY,
      7,
    );
    expect(groups.map((g) => [g.label, g.events.map((e) => e.title)])).toEqual([
      ['Today', ['Standup']],
      ['Tomorrow', ['Dentist']],
      ['Thu 14 Mar', ['Trip']],
      ['Fri 15 Mar', ['Trip']],
    ]);
    expect(eventWhen(groups[0].events[0])).toBe('09:00–09:15');
  });
});
