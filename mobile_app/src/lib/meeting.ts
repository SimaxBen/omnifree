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

/** Calls `POST /notes/meeting` (final) until the note is written; each call is one bounded hub run. */
export async function finishMeeting(
  client: HubClient,
  meetingId: string,
  onStatus: (s: MeetingStatus) => void,
  signal?: AbortSignal,
  wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  maxCalls = 120,
): Promise<MeetingStatus> {
  let last: MeetingStatus | null = null;
  for (let i = 0; i < maxCalls; i++) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    last = await postMeeting(client, { meeting_id: meetingId, final: true }, signal);
    onStatus(last);
    if (last.status === 'done') return last;
    if (last.busy) await wait(3000); // another call is still working on it
  }
  throw new Error(`The meeting is still being processed (${last?.parts_done ?? 0}/${last?.parts_total ?? 0} parts); open it again later`);
}
