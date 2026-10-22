// Meeting recordings: hub calls for /notes/meeting and pure helpers. The recorder (views/notes/MeetingPanel)
// restarts MediaRecorder every PART_SECONDS so each part is a complete, independently decodable audio file
// (MediaRecorder timeslice chunks are not: only the first one carries the container header). Each part goes
// to the Drive (straight to B2/R2 through a presigned URL above the hub's body limit) and is announced to the
// hub, which transcribes the parts in the background of the meeting and writes the note at the end.

import type { HubClient } from './api';
import type { Note, Task } from './types';

/** Length of one recorded part: 10 minutes of 32 kbit/s Opus is about 2.4 MB, far below Whisper's 25 MB. */
export const PART_SECONDS = 600;
export const AUDIO_BITS_PER_SECOND = 32_000;

export interface MeetingPart {
  key: string;
  provider?: string;
  seconds?: number;
}

export type MeetingState = 'transcribing' | 'waiting' | 'summarizing' | 'done';

export interface MeetingStatus {
  meeting_id: string;
  status: MeetingState;
  final: boolean;
  parts_total: number;
  parts_done: number;
  parts_failed: number;
  remaining: number;
  note_id: string | null;
  task_ids: string[];
  error: string | null;
  busy?: boolean;
  note?: Note | null;
  tasks?: Task[];
  summary?: string;
  decisions?: string[];
}

export interface MeetingRequest {
  meeting_id?: string;
  audio_keys?: MeetingPart[];
  final?: boolean;
  title?: string;
  language?: string;
  notebook?: string;
}

/** `POST /notes/meeting`: add parts and/or let the hub work on the meeting for one call (under 60 s). */
export function postMeeting(client: HubClient, req: MeetingRequest, signal?: AbortSignal): Promise<MeetingStatus> {
  return client.request<MeetingStatus>('POST', '/notes/meeting', { json: req, signal });
}

export function meetingStatus(client: HubClient, id: string, signal?: AbortSignal): Promise<MeetingStatus> {
  return client.request<MeetingStatus>('GET', `/notes/meeting/${encodeURIComponent(id)}`, { signal });
}

/** Drive key of a part: `meetings/<yyyy>/<mm>/<session>/part-007.webm`. */
export function meetingPartKey(session: string, index: number, ext: string, day: string): string {
  const [y, m] = day.split('-');
  return `meetings/${y}/${m}/${session}/part-${String(index).padStart(3, '0')}.${ext.replace(/^\./, '') || 'webm'}`;
}

/** A short random id for the parts' folder (the hub's meeting id only exists after the first part). */
export function newSession(now = Date.now(), rand = Math.random): string {
  return `${now.toString(36)}${Math.floor(rand() * 36 ** 4).toString(36).padStart(4, '0')}`;
}

/** The recorder format: Opus in WebM when supported (Chrome/Brave), else the browser's default. */
export function pickMime(isSupported: (t: string) => boolean): string | undefined {
  return ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm', 'audio/mp4'].find((t) => {
    try {
      return isSupported(t);
    } catch {
      return false;
    }
  });
}

/** 0..1 progress of the hub's work: transcription first (90 %), then the summary. */
export function meetingProgress(s: Pick<MeetingStatus, 'status' | 'parts_total' | 'parts_done'> | null): number {
  if (!s) return 0;
  if (s.status === 'done') return 1;
  const share = s.parts_total ? s.parts_done / s.parts_total : 0;
  return Math.min(0.95, share * 0.9 + (s.status === 'summarizing' ? 0.05 : 0));
}

export function meetingStatusText(s: Pick<MeetingStatus, 'status' | 'parts_total' | 'parts_done' | 'busy'> | null): string {
  if (!s) return 'Starting…';
  if (s.status === 'done') return 'Note ready';
  if (s.busy) return 'Working on it…';
  if (s.status === 'summarizing') return 'Writing the summary and action items';
  const n = s.parts_total;
  return `Transcribing part ${Math.min(s.parts_done + 1, n)} of ${n}`;
}

/**
 * Calls `POST /notes/meeting` (final) until the note is written; each call is one bounded hub run. `parts` (every
 * uploaded part) go with the first call: the hub ignores the keys it already has and adds any it missed (a part
 * whose own call failed), and creates the meeting when `meetingId` is still null.
 */
export async function finishMeeting(
  client: HubClient,
  meetingId: string | null,
  onStatus: (s: MeetingStatus) => void,
  signal?: AbortSignal,
  wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  maxCalls = 120,
  parts: MeetingPart[] = [],
): Promise<MeetingStatus> {
  let last: MeetingStatus | null = null;
  let id = meetingId;
  for (let i = 0; i < maxCalls; i++) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const keys = i === 0 && parts.length ? { audio_keys: parts } : {};
    last = await postMeeting(client, { ...(id ? { meeting_id: id } : {}), ...keys, final: true }, signal);
    id = last.meeting_id;
    onStatus(last);
    if (last.status === 'done') return last;
    if (last.busy) await wait(3000); // another call is still working on it
  }
  throw new Error(`The meeting is still being processed (${last?.parts_done ?? 0}/${last?.parts_total ?? 0} parts); open it again later`);
}

// ------------------------------------------------------------------ unfinished meetings (survive a closed sheet or a reload)

/** A meeting whose note is not written yet: kept in localStorage until the hub answers `done`. */
export interface PendingMeeting {
  meetingId: string | null;
  session: string;
  title?: string;
  startedAt: number;
  parts: MeetingPart[];
}

const PENDING_KEY = 'omnifree.meeting.pending';

type KV = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function store(): KV | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function loadPendingMeeting(kv: KV | null = store()): PendingMeeting | null {
  try {
    const raw = kv?.getItem(PENDING_KEY);
    const p = raw ? (JSON.parse(raw) as Partial<PendingMeeting>) : null;
    if (!p || typeof p.session !== 'string' || !Array.isArray(p.parts)) return null;
    const parts = p.parts.filter((x): x is MeetingPart => !!x && typeof x.key === 'string');
    if (!parts.length && !p.meetingId) return null;
    return { meetingId: typeof p.meetingId === 'string' ? p.meetingId : null, session: p.session, title: p.title, startedAt: Number(p.startedAt) || 0, parts };
  } catch {
    return null;
  }
}

export function savePendingMeeting(p: PendingMeeting, kv: KV | null = store()): void {
  try {
    kv?.setItem(PENDING_KEY, JSON.stringify(p));
  } catch {
    // Storage full or blocked: only the resume entry is lost.
  }
}

export function clearPendingMeeting(kv: KV | null = store()): void {
  try {
    kv?.removeItem(PENDING_KEY);
  } catch {
    // Same as above.
  }
}

/** Records one more uploaded part (and the hub's meeting id once known) for `session`. */
export function rememberPart(p: PendingMeeting | null, session: string, part: MeetingPart | null, meetingId: string | null, title?: string, now = Date.now()): PendingMeeting {
  const base = p && p.session === session ? p : { meetingId: null, session, startedAt: now, parts: [] };
  const parts = part && !base.parts.some((x) => x.key === part.key) ? [...base.parts, part] : base.parts;
  return { ...base, meetingId: meetingId ?? base.meetingId, parts, ...(title ? { title } : {}) };
}
