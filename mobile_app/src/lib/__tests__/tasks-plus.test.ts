import { describe, expect, it } from 'vitest';
import {
  activeBlock,
  addItem,
  checklistProgress,
  cleanChecklist,
  eventSlot,
  minutesLabel,
  moveItem,
  newItemId,
  recurrenceLabel,
  removeItem,
  renameItem,
  repeatDraft,
  ruleFromDraft,
  slotLabel,
  toggleDay,
  toggleItem,
} from '../tasks';
import type { ChecklistItem } from '../types';

const TODAY = '2030-03-12'; // a Tuesday

describe('recurrence', () => {
  it('labels rules like the hub', () => {
    expect(recurrenceLabel({ freq: 'daily' })).toBe('Daily');
    expect(recurrenceLabel({ freq: 'weekly', days: [3, 0] })).toBe('Every Mon, Thu');
    expect(recurrenceLabel({ freq: 'weekly', days: [0, 1, 2, 3, 4] })).toBe('Weekdays');
    expect(recurrenceLabel({ freq: 'weekly', days: [5, 6] })).toBe('Weekends');
    expect(recurrenceLabel({ freq: 'monthly', day: 1 })).toBe('Monthly on the 1st');
    expect(recurrenceLabel({ freq: 'monthly', day: 22 })).toBe('Monthly on the 22nd');
    expect(recurrenceLabel({ freq: 'monthly', day: 13 })).toBe('Monthly on the 13th');
    expect(recurrenceLabel({ freq: 'interval', interval: 3, until: '2030-05-01' })).toBe('Every 3 days until 2030-05-01');
    expect(recurrenceLabel({ freq: 'yearly', month: 3, day: 12 })).toBe('Yearly on Mar 12');
    expect(recurrenceLabel(null)).toBe('');
  });

  it('round-trips the picker state', () => {
    const none = repeatDraft(null, '2030-03-14', TODAY);
    expect(none).toEqual({ kind: 'none', days: [3], day: 14, interval: 2, until: '' }); // Thursday the 14th
    expect(ruleFromDraft(none, '2030-03-14', TODAY)).toBeNull();
    expect(ruleFromDraft({ ...none, kind: 'weekly' }, '2030-03-14', TODAY)).toEqual({ freq: 'weekly', days: [3] });
    expect(ruleFromDraft({ ...none, kind: 'weekly', days: [] }, null, TODAY)).toEqual({ freq: 'weekly', days: [1] });
    expect(ruleFromDraft({ ...none, kind: 'monthly', day: 40 }, null, TODAY)).toEqual({ freq: 'monthly', day: 31 });
    expect(ruleFromDraft({ ...none, kind: 'interval', interval: 1 }, null, TODAY)).toEqual({ freq: 'interval', interval: 2 });
    expect(ruleFromDraft({ ...none, kind: 'yearly' }, '2030-07-04', TODAY)).toEqual({ freq: 'yearly', month: 7, day: 4 });
    expect(ruleFromDraft({ ...none, kind: 'daily', until: '2030-04-01' }, null, TODAY)).toEqual({ freq: 'daily', until: '2030-04-01' });
    const back = repeatDraft({ freq: 'weekly', days: [4, 0] }, TODAY, TODAY);
    expect(back.kind).toBe('weekly');
    expect(back.days).toEqual([0, 4]);
    expect(repeatDraft({ freq: 'interval', interval: 5, until: '2030-06-01' }, null, TODAY)).toMatchObject({ kind: 'interval', interval: 5, until: '2030-06-01' });
  });

  it('never empties the weekday selection', () => {
    expect(toggleDay([0], 2)).toEqual([0, 2]);
    expect(toggleDay([0, 2], 0)).toEqual([2]);
    expect(toggleDay([2], 2)).toEqual([2]);
  });
});

describe('checklist', () => {
  const items: ChecklistItem[] = [
    { id: 'a', text: 'passport', done: true },
    { id: 'b', text: 'tickets', done: false },
    { id: 'c', text: 'adapter', done: false },
  ];

  it('counts progress', () => {
    expect(checklistProgress(items)).toEqual({ done: 1, total: 3, label: '1/3' });
    expect(checklistProgress([])).toEqual({ done: 0, total: 0, label: '' });
    expect(checklistProgress(undefined).label).toBe('');
  });

  it('adds, toggles, renames, removes and reorders', () => {
    const added = addItem(items, '  sun   cream ', 'd');
    expect(added.at(-1)).toEqual({ id: 'd', text: 'sun cream', done: false });
    expect(addItem(items, '   ')).toBe(items);
    expect(toggleItem(items, 'b')[1].done).toBe(true);
    expect(renameItem(items, 'c', 'plug')[2].text).toBe('plug');
    expect(removeItem(items, 'a').map((i) => i.id)).toEqual(['b', 'c']);
    expect(moveItem(items, 2, -1).map((i) => i.id)).toEqual(['a', 'c', 'b']);
    expect(moveItem(items, 0, 1).map((i) => i.id)).toEqual(['b', 'a', 'c']);
    expect(moveItem(items, 0, -1)).toBe(items);
    expect(moveItem(items, 5, 1)).toBe(items);
    expect(cleanChecklist([{ id: 'x', text: '  a  b ', done: false }, { id: 'y', text: ' ', done: true }])).toEqual([{ id: 'x', text: 'a b', done: false }]);
  });

  it('makes ids the hub keeps', () => {
    const ids = new Set(Array.from({ length: 50 }, () => newItemId()));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(/^[A-Za-z0-9_-]{1,24}$/);
  });
});

describe('time blocks', () => {
  it('formats estimates', () => {
    expect(minutesLabel(45)).toBe('45 min');
    expect(minutesLabel(60)).toBe('1 h');
    expect(minutesLabel(90)).toBe('1 h 30');
    expect(minutesLabel(125)).toBe('2 h 05');
    expect(minutesLabel(null)).toBe('');
  });

  it('labels slots relative to today', () => {
    expect(slotLabel({ start: '2030-03-12T10:30', end: '2030-03-12T11:30' }, TODAY)).toBe('Today 10:30–11:30');
    expect(slotLabel({ start: '2030-03-13T09:00', end: '2030-03-13T09:30' }, TODAY)).toBe('Tomorrow 09:00–09:30');
    expect(slotLabel({ start: '2030-03-14T15:00', end: null }, TODAY)).toBe('Thu 15:00');
    expect(slotLabel({ start: '2030-03-25T15:00', end: '2030-03-25T16:00' }, TODAY)).toBe('Mon 25 Mar 15:00–16:00');
    expect(slotLabel({ start: null, end: null }, TODAY)).toBe('');
  });

  it('keeps only blocks still ahead or under way', () => {
    const block = { event_id: 'e', start: '2030-03-12T10:00', end: '2030-03-12T11:00' };
    expect(activeBlock({ block }, '2030-03-12T10:30')).toBe(block);
    expect(activeBlock({ block }, '2030-03-12T11:00')).toBeNull();
    expect(activeBlock({ block: null }, '2030-03-12T10:30')).toBeNull();
  });

  it('turns a calendar event into local slot bounds', () => {
    expect(eventSlot({ start: '2030-03-14T10:00:00+01:00', end: '2030-03-14T11:00:00+01:00', all_day: false }, 'Africa/Casablanca')).toEqual({
      start: '2030-03-14T10:00',
      end: '2030-03-14T11:00',
    });
    expect(eventSlot({ start: '2030-03-14T09:00:00Z', end: '', all_day: false }, 'UTC')).toEqual({ start: '2030-03-14T09:00', end: null });
    expect(eventSlot({ start: '2030-03-20', end: '2030-03-21', all_day: true })).toEqual({ start: '2030-03-20T00:00', end: null });
  });
});
