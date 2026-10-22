// Tasks & calendar helpers: recurrence picker <-> rule, checklist edits, time-block labels. Pure, no I/O.
import { HOME_TZ, clockTime, isoDay } from './apps';
import type { ChecklistItem, GCalEvent, Recurrence, Task } from './types';

const DAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const;
const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'] as const;

export { DAY_SHORT };

function ordinal(n: number): string {
  const s = n % 100 >= 10 && n % 100 <= 20 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th';
  return `${n}${s}`;
}

/** Short English label, same wording as the hub's `recurrence_label`: "Daily", "Every Mon, Thu", "Monthly on the 1st". */
export function recurrenceLabel(rule: Recurrence | null | undefined): string {
  if (!rule) return '';
  let label: string;
  switch (rule.freq) {
    case 'daily':
      label = 'Daily';
      break;
    case 'weekly': {
      const days = [...(rule.days ?? [])].sort((a, b) => a - b);
      const key = days.join(',');
      label = key === '0,1,2,3,4' ? 'Weekdays' : key === '5,6' ? 'Weekends' : `Every ${days.map((d) => DAY_SHORT[d]).join(', ')}`;
      break;
    }
    case 'monthly':
      label = `Monthly on the ${ordinal(rule.day ?? 1)}`;
      break;
    case 'interval':
      label = `Every ${rule.interval ?? 2} days`;
      break;
    case 'yearly':
      label = `Yearly on ${MONTH_SHORT[(rule.month ?? 1) - 1]} ${rule.day ?? 1}`;
      break;
    default:
      return '';
  }
  return rule.until ? `${label} until ${rule.until}` : label;
}

export type RepeatKind = 'none' | Recurrence['freq'];

/** The editor's recurrence picker state. */
export interface RepeatDraft {
  kind: RepeatKind;
  days: number[];
  day: number;
  interval: number;
  until: string;
}

function weekday(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number);
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7; // 0 = Monday
}

/** Picker state for a task: its rule, with sensible defaults (the due date's weekday / day of month) for the rest. */
export function repeatDraft(rule: Recurrence | null | undefined, due: string | null | undefined, today: string): RepeatDraft {
  const anchor = due || today;
  const base: RepeatDraft = { kind: 'none', days: [weekday(anchor)], day: Number(anchor.slice(8, 10)), interval: 2, until: '' };
  if (!rule) return base;
  return {
    kind: rule.freq,
    days: rule.freq === 'weekly' && rule.days?.length ? [...rule.days].sort((a, b) => a - b) : base.days,
    day: rule.day ?? base.day,
    interval: rule.interval ?? base.interval,
    until: rule.until ?? '',
  };
}

/** The rule to send for a picker state (null = not recurring). Yearly takes the due date's month and day. */
export function ruleFromDraft(d: RepeatDraft, due: string | null | undefined, today: string): Recurrence | null {
  const anchor = due || today;
  const until = d.until ? { until: d.until } : {};
  switch (d.kind) {
    case 'none':
      return null;
    case 'daily':
      return { freq: 'daily', ...until };
    case 'weekly':
      return { freq: 'weekly', days: d.days.length ? [...new Set(d.days)].sort((a, b) => a - b) : [weekday(anchor)], ...until };
    case 'monthly':
      return { freq: 'monthly', day: Math.min(31, Math.max(1, Math.round(d.day) || 1)), ...until };
    case 'interval': {
      const n = Math.min(366, Math.max(2, Math.round(d.interval) || 2));
      return { freq: 'interval', interval: n, ...until };
    }
    case 'yearly':
      return { freq: 'yearly', month: Number(anchor.slice(5, 7)), day: Number(anchor.slice(8, 10)), ...until };
  }
}

/** Toggle one weekday in the picker (never leaves it empty). */
export function toggleDay(days: number[], day: number): number[] {
  const next = days.includes(day) ? days.filter((d) => d !== day) : [...days, day];
  return next.length ? next.sort((a, b) => a - b) : days;
}

// ------------------------------------------------------------------ checklist

export function checklistProgress(items: ChecklistItem[] | null | undefined): { done: number; total: number; label: string } {
  const list = items ?? [];
  const done = list.filter((i) => i.done).length;
  return { done, total: list.length, label: list.length ? `${done}/${list.length}` : '' };
}

let seq = 0;
/** A client-side id for a new item (the hub keeps any id of 1-24 letters, digits, - or _). */
export function newItemId(): string {
  seq = (seq + 1) % 1000;
  return `n${Date.now().toString(36)}${seq.toString(36)}`.slice(0, 24);
}

export function addItem(items: ChecklistItem[], text: string, id: string = newItemId()): ChecklistItem[] {
  const t = text.replace(/\s+/g, ' ').trim();
  return t ? [...items, { id, text: t.slice(0, 300), done: false }] : items;
}

export function toggleItem(items: ChecklistItem[], id: string): ChecklistItem[] {
  return items.map((i) => (i.id === id ? { ...i, done: !i.done } : i));
}

export function renameItem(items: ChecklistItem[], id: string, text: string): ChecklistItem[] {
  return items.map((i) => (i.id === id ? { ...i, text } : i));
}

export function removeItem(items: ChecklistItem[], id: string): ChecklistItem[] {
  return items.filter((i) => i.id !== id);
}

/** Move an item by `delta` places (clamped); the same list when it cannot move. */
export function moveItem<T>(items: T[], index: number, delta: number): T[] {
  const to = Math.max(0, Math.min(items.length - 1, index + delta));
  if (index < 0 || index >= items.length || to === index) return items;
  const next = [...items];
  const [it] = next.splice(index, 1);
  next.splice(to, 0, it);
  return next;
}

/** After an item was added on the hub: keep the local list (and its unsaved renames/order), append what is new. */
export function mergeAdded(local: ChecklistItem[], server: ChecklistItem[] | null | undefined): ChecklistItem[] {
  const ids = new Set(local.map((i) => i.id));
  return [...local, ...(server ?? []).filter((i) => !ids.has(i.id))];
}

/** Checklist items worth sending: trimmed, blanks dropped. */
export function cleanChecklist(items: ChecklistItem[]): ChecklistItem[] {
  return items.map((i) => ({ ...i, text: i.text.replace(/\s+/g, ' ').trim() })).filter((i) => i.text);
}

// ------------------------------------------------------------------ time blocks

/** "45 min", "1 h", "1 h 30". */
export function minutesLabel(min: number | null | undefined): string {
  if (!min || min <= 0) return '';
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (!h) return `${m} min`;
  return m ? `${h} h ${String(m).padStart(2, '0')}` : `${h} h`;
}

export const ESTIMATES = [15, 30, 45, 60, 90, 120] as const;

function dayLabel(day: string, today: string): string {
  const [y, m, d] = day.split('-').map(Number);
  const [ty, tm, td] = today.split('-').map(Number);
  const diff = Math.round((Date.UTC(y, m - 1, d) - Date.UTC(ty, tm - 1, td)) / 86_400_000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  const date = new Date(Date.UTC(y, m - 1, d));
  if (diff > 1 && diff < 7) return date.toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' });
  return date.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
}

/** "Today 10:30–11:30", "Thu 14:00–15:00" for local `YYYY-MM-DDTHH:MM` bounds. */
export function slotLabel(slot: { start: string | null; end: string | null }, today: string): string {
  if (!slot.start) return '';
  const day = slot.start.slice(0, 10);
  const from = slot.start.slice(11, 16);
  const to = slot.end && slot.end.slice(0, 10) === day ? slot.end.slice(11, 16) : '';
  return `${dayLabel(day, today)} ${from}${to ? `–${to}` : ''}`;
}

/** The time block of a task, when it is still ahead or under way (a past block is history). */
export function activeBlock(task: Pick<Task, 'block'>, nowLocal: string): Task['block'] {
  const b = task.block;
  if (!b?.start) return null;
  return (b.end ?? b.start) > nowLocal ? b : null;
}

/** Local `YYYY-MM-DDTHH:MM` bounds of a calendar event, for slotLabel (an all-day event starts at 00:00). */
export function eventSlot(ev: Pick<GCalEvent, 'start' | 'end' | 'all_day'>, tz = HOME_TZ): { start: string | null; end: string | null } {
  if (ev.all_day) return { start: `${ev.start.slice(0, 10)}T00:00`, end: null };
  const at = (iso: string) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : `${isoDay(d, tz)}T${clockTime(d, tz)}`;
  };
  return { start: at(ev.start), end: ev.end ? at(ev.end) : null };
}
