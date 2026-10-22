import { describe, expect, it } from 'vitest';
import { clockTime, dueInfo, sortTasks, taskDue, validTime } from '../apps';
import type { Task } from '../types';

const task = (id: string, over: Partial<Task>): Task => ({
  id,
  title: id,
  details: null,
  due: null,
  due_time: null,
  done: false,
  priority: 'normal',
  note_id: null,
  created_at: '2026-10-01',
  done_at: null,
  ...over,
});

describe('task times', () => {
  it('formats the clock in the home time zone, UTC+1 or UTC+0 during Ramadan', () => {
    expect(clockTime(new Date('2026-10-20T14:05:00Z'))).toBe('15:05');
    expect(clockTime(new Date('2026-10-20T23:30:00Z'))).toBe('00:30');
    expect(clockTime(new Date('2026-03-01T14:05:00Z'))).toBe('14:05'); // Ramadan 2026: UTC+0
    expect(clockTime(new Date('2026-10-20T14:05:00Z'), 'UTC')).toBe('14:05');
  });

  it('accepts only HH:MM times', () => {
    expect(validTime('15:00')).toBe('15:00');
    expect(validTime('00:00')).toBe('00:00');
    for (const bad of ['24:00', '9:00', '15:60', '15h', '', null, undefined]) expect(validTime(bad)).toBeNull();
  });

  it('adds the time to the due label', () => {
    const today = '2026-10-20';
    expect(dueInfo('2026-10-20', today, '15:00', '09:00')).toEqual({ label: 'Today · 15:00', tone: 'today' });
    expect(dueInfo('2026-10-21', today, '08:30')).toEqual({ label: 'Tomorrow · 08:30', tone: 'soon' });
    expect(dueInfo('2026-10-23', today, '10:00')).toEqual({ label: 'Friday · 10:00', tone: 'soon' });
    expect(dueInfo('2026-11-30', today, '18:45')).toEqual({ label: '30 Nov · 18:45', tone: 'later' });
    expect(dueInfo('2026-10-19', today, '18:45')).toEqual({ label: 'Yesterday · 18:45', tone: 'overdue' });
    expect(dueInfo('2026-10-20', today, 'bogus', '09:00')).toEqual({ label: 'Today', tone: 'today' });
    expect(dueInfo(null, today, '15:00')).toEqual({ label: 'No date', tone: 'none' });
  });

  it('counts the time in the overdue logic', () => {
    const today = '2026-10-20';
    expect(dueInfo('2026-10-20', today, '15:00', '15:01').tone).toBe('overdue');
    expect(dueInfo('2026-10-20', today, '15:00', '15:00').tone).toBe('today'); // due now: not late yet
    expect(dueInfo('2026-10-20', today, '15:00', '14:59').tone).toBe('today');
    expect(dueInfo('2026-10-20', today, null, '23:59').tone).toBe('today'); // all-day: late only tomorrow
    expect(dueInfo('2026-10-20', today, '15:00').tone).toBe('today'); // no clock given
  });

  it('labels a task at the current home time', () => {
    const now = new Date('2026-10-20T15:30:00Z'); // 16:30 in Casablanca
    expect(taskDue(task('a', { due: '2026-10-20', due_time: '16:00' }), now)).toEqual({ label: 'Today · 16:00', tone: 'overdue' });
    expect(taskDue(task('b', { due: '2026-10-20', due_time: '17:00' }), now)).toEqual({ label: 'Today · 17:00', tone: 'today' });
    expect(taskDue(task('c', { due: '2026-10-20' }), now)).toEqual({ label: 'Today', tone: 'today' });
    expect(taskDue(task('d', {}), now).tone).toBe('none');
  });

  it('sorts by date, then time (untimed last on a day), then priority', () => {
    const sorted = sortTasks([
      task('nodate', {}),
      task('d2-allday-high', { due: '2026-10-21', priority: 'high' }),
      task('d2-18h', { due: '2026-10-21', due_time: '18:00' }),
      task('d2-0830', { due: '2026-10-21', due_time: '08:30', priority: 'low' }),
      task('d1-23h', { due: '2026-10-20', due_time: '23:00' }),
      task('d2-allday', { due: '2026-10-21' }),
      task('done', { done: true, due: '2026-10-01', due_time: '07:00' }),
      task('d2-0830-high', { due: '2026-10-21', due_time: '08:30', priority: 'high' }),
    ]);
    expect(sorted.map((x) => x.id)).toEqual(['d1-23h', 'd2-0830-high', 'd2-0830', 'd2-18h', 'd2-allday-high', 'd2-allday', 'nodate', 'done']);
  });

  it('copes with tasks from an older hub that have no due_time field', () => {
    const old = { ...task('old', { due: '2026-10-21' }) } as Partial<Task>;
    delete old.due_time;
    expect(sortTasks([old as Task, task('timed', { due: '2026-10-21', due_time: '09:00' })]).map((x) => x.id)).toEqual(['timed', 'old']);
    expect(taskDue(old as Task, new Date('2026-10-21T10:00:00Z')).label).toBe('Today');
  });
});
