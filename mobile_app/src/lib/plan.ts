// "Plan my day": the hub calls for /plan/today and pure helpers for the Home timeline. No React here.

import type { HubClient } from './api';

export type PlanBlockKind = 'event' | 'task' | 'break';

export interface PlanBlock {
  /** HH:MM, Africa/Casablanca. */
  start: string;
  end: string;
  kind: PlanBlockKind;
  /** Task id or Google event id; null for breaks. */
  ref_id: string | null;
  title: string;
  reason: string;
  /** A calendar event or a task with its own time: not moved by the planner. */
  fixed: boolean;
  /** Task blocks: the task has been ticked since the plan was made. */
  done?: boolean;
}

export interface DayPlan {
  day: string;
  status: 'none' | 'proposed' | 'accepted';
  source: 'llm' | 'greedy' | null;
  blocks: PlanBlock[];
  unscheduled: { id: string; title: string; reason: string }[];
  /** Blocks booked in Google Calendar. */
  booked: number;
  window: { start: string; end: string } | null;
  generated_at: string | null;
  calendar: { configured: boolean; writable: boolean } | null;
  /** Set when Google Calendar could not be read: the plan ignores the day's events. */
  agenda_error: string | null;
  /** After a re-plan of a booked plan: whether its calendar blocks were all removed. */
  cleared_previous?: boolean;
  errors?: string[];
}

export function normalizePlan(body: unknown): DayPlan {
  const b = (typeof body === 'object' && body !== null ? body : {}) as Partial<DayPlan>;
  return {
    day: typeof b.day === 'string' ? b.day : '',
    status: b.status === 'proposed' || b.status === 'accepted' ? b.status : 'none',
    source: b.source === 'llm' || b.source === 'greedy' ? b.source : null,
    blocks: Array.isArray(b.blocks) ? b.blocks.filter((x): x is PlanBlock => !!x && typeof x.start === 'string' && typeof x.end === 'string') : [],
    unscheduled: Array.isArray(b.unscheduled) ? b.unscheduled : [],
    booked: typeof b.booked === 'number' ? b.booked : 0,
    window: b.window ?? null,
    generated_at: b.generated_at ?? null,
    calendar: b.calendar ?? null,
    agenda_error: typeof b.agenda_error === 'string' && b.agenda_error ? b.agenda_error : null,
    cleared_previous: typeof b.cleared_previous === 'boolean' ? b.cleared_previous : undefined,
    errors: Array.isArray(b.errors) ? b.errors : undefined,
  };
}

export async function getPlan(client: HubClient, signal?: AbortSignal): Promise<DayPlan> {
  return normalizePlan(await client.request<unknown>('GET', '/plan/today', { signal }));
}

/** `POST /plan/today`: a fresh plan (one LLM call on the hub, a greedy plan when it fails). */
export async function makePlan(client: HubClient, useLlm = true): Promise<DayPlan> {
  return normalizePlan(await client.request<unknown>('POST', '/plan/today', { json: { use_llm: useLlm } }));
}

/** `POST /plan/today/accept`: `book` also puts the task blocks in Google Calendar. */
export async function acceptPlan(client: HubClient, book: boolean): Promise<DayPlan> {
  return normalizePlan(await client.request<unknown>('POST', '/plan/today/accept', { json: { book } }));
}

/** `DELETE /plan/today`: removes the booked calendar blocks and the plan. */
export function clearPlan(client: HubClient): Promise<{ cleared: boolean; deleted_events: number; error: string | null }> {
  return client.request('DELETE', '/plan/today');
}

export function minutesOf(hhmm: string): number {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
}

/** "1 h 30", "45 min". */
export function durationLabel(block: Pick<PlanBlock, 'start' | 'end'>): string {
  const d = Math.max(0, minutesOf(block.end) - minutesOf(block.start));
  const h = Math.floor(d / 60);
  const m = d % 60;
  return h ? `${h} h${m ? ` ${String(m).padStart(2, '0')}` : ''}` : `${m} min`;
}

/** Counts for the card header: planned task blocks, focus minutes, events. */
export function planStats(plan: DayPlan): { tasks: number; focusMinutes: number; events: number; done: number } {
  let tasks = 0;
  let focus = 0;
  let events = 0;
  let done = 0;
  for (const b of plan.blocks) {
    if (b.kind === 'task') {
      tasks++;
      focus += Math.max(0, minutesOf(b.end) - minutesOf(b.start));
      if (b.done) done++;
    } else if (b.kind === 'event') events++;
  }
  return { tasks, focusMinutes: focus, events, done };
}

/** Index of the block happening at `now` (HH:MM), else of the next one; -1 when the day's blocks are over. */
export function currentBlockIndex(blocks: PlanBlock[], now: string): number {
  const t = minutesOf(now);
  const at = blocks.findIndex((b) => minutesOf(b.start) <= t && t < minutesOf(b.end));
  if (at >= 0) return at;
  return blocks.findIndex((b) => minutesOf(b.start) > t);
}

/**
 * The collapsed timeline: `size` blocks around the live one (one finished block kept for context), the last ones
 * when the day's blocks are over. `current` is the live block's index in `blocks` (-1: none).
 */
export function visibleWindow(blocks: PlanBlock[], now: string, size: number): { start: number; end: number; current: number } {
  const current = currentBlockIndex(blocks, now);
  if (blocks.length <= size) return { start: 0, end: blocks.length, current };
  const from = current < 0 ? blocks.length - size : Math.min(Math.max(0, current - 1), blocks.length - size);
  return { start: from, end: from + size, current };
}

/** Whether the plan can still go to Google Calendar: writable calendar and at least one flexible task block. */
export function canBook(plan: DayPlan): boolean {
  return !!plan.calendar?.writable && plan.blocks.some((b) => b.kind === 'task' && !b.fixed);
}
