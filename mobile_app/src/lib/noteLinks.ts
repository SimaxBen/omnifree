// Linked notes: the hub calls for [[wiki links]], backlinks and the #tag filter, plus a tiny channel that lets a
// tapped #tag in a note open the Notes list filtered by it.

import type { HubClient } from './api';
import { listOf } from './api';
import type { Note, NoteListing, NotebookCount } from './types';

export interface Backlink {
  id: string;
  title: string;
  notebook: string;
  updated_at: string;
  /** Text around the link in the linking note. */
  snippet: string;
}

export interface OutgoingLink {
  title: string;
  /** null: no note has that title yet (tapping the link creates it). */
  note_id: string | null;
}

export interface NoteLinks {
  note_id: string;
  title: string;
  backlinks: Backlink[];
  outgoing: OutgoingLink[];
}

export interface TitleHit {
  id: string;
  title: string;
  notebook: string;
}

/** `GET /notes/{id}/backlinks`. */
export async function noteBacklinks(client: HubClient, id: string, signal?: AbortSignal): Promise<NoteLinks> {
  const body = await client.request<Partial<NoteLinks>>('GET', `/notes/${encodeURIComponent(id)}/backlinks`, { signal });
  return {
    note_id: body?.note_id ?? id,
    title: body?.title ?? '',
    backlinks: Array.isArray(body?.backlinks) ? body.backlinks : [],
    outgoing: Array.isArray(body?.outgoing) ? body.outgoing : [],
  };
}

/** `GET /notes/links/titles`: note titles for the `[[` autocomplete. */
export async function noteTitles(client: HubClient, q: string, signal?: AbortSignal, limit = 6): Promise<TitleHit[]> {
  const body = await client.request<unknown>('GET', '/notes/links/titles', { query: { q: q.trim(), limit }, signal });
  return listOf<TitleHit>(body, 'titles');
}

/** `POST /notes/links/open`: the note a [[link]] points at, created (empty, with that title) when missing. */
export function openNoteLink(client: HubClient, title: string, notebook?: string): Promise<{ note: Note; created: boolean }> {
  return client.request('POST', '/notes/links/open', { json: { title, create: true, ...(notebook ? { notebook } : {}) } });
}

/** `GET /notes?tag=`: notes with a tag (in their tag list or written as #tag in the body), narrowed by `q` when set. */
export async function notesByTag(client: HubClient, tag: string, opts: { notebook?: string; limit?: number; q?: string } = {}, signal?: AbortSignal): Promise<NoteListing> {
  const q = opts.q?.trim() || undefined;
  const body = await client.request<unknown>('GET', '/notes', { query: { tag: normalizeTag(tag), notebook: opts.notebook, limit: opts.limit, ...(q ? { q } : {}) }, signal });
  const books = typeof body === 'object' && body !== null && Array.isArray((body as { notebooks?: unknown }).notebooks) ? (body as { notebooks: NotebookCount[] }).notebooks : [];
  return { notes: listOf<Note>(body, 'notes'), notebooks: books };
}

/** `#Idea ` -> `idea`. */
export function normalizeTag(tag: string): string {
  return tag.trim().replace(/^#+/, '').toLowerCase();
}

// ------------------------------------------------------------------ "filter the notes list by this tag"

let pendingTag: string | null = null;
const listeners = new Set<(tag: string) => void>();

/** Asks the Notes list to show the notes with `tag` (the list may not be mounted yet: it reads it on mount). */
export function requestTagFilter(tag: string): void {
  pendingTag = normalizeTag(tag);
  for (const l of [...listeners]) l(pendingTag);
}

/** The tag asked for before the Notes list mounted (read once). */
export function takePendingTag(): string | null {
  const t = pendingTag;
  pendingTag = null;
  return t;
}

export function onTagFilter(fn: (tag: string) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
