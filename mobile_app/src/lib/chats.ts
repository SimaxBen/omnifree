// Saved AI conversations: pure helpers (titles, history trimming, date groups) and the local
// queue of conversations the hub has not accepted yet (offline, hub down). The queue lives in
// localStorage through readPref/writePref, so a failed save survives a reload and is retried.

import { HubError, type HubClient } from './api';
import { addDays, isoDay } from './apps';
import { readPref, writePref } from './settings';
import type { ChatMessage, ChatSummary, ChatThread, ChatThreadMessage } from './types';

export const NEW_CHAT_TITLE = 'New chat';
/** Roughly 6k tokens of prior conversation: fits every free model's context with room for the reply. */
export const HISTORY_CHAR_BUDGET = 24_000;
export const HISTORY_MAX_MESSAGES = 60;

const PENDING_KEY = 'chat.pending';
const ID_ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';

/** A client-chosen conversation id (the hub's `PUT /chats/{id}` accepts 6-64 of [A-Za-z0-9_-]). */
export function newChatId(): string {
  const bytes = new Uint8Array(14);
  try {
    crypto.getRandomValues(bytes);
  } catch {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  return `c${Array.from(bytes, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join('')}`;
}

/** Same rule as the hub: the first user message, whitespace collapsed, at most `limit` characters. */
export function chatTitleFrom(messages: { role: string; content: string | null }[], limit = 60): string {
  for (const m of messages) {
    if (m.role !== 'user') continue;
    const text = (m.content ?? '').replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim();
    if (text) return text.length <= limit ? text : `${text.slice(0, limit - 1).trimEnd()}…`;
  }
  return NEW_CHAT_TITLE;
}

/**
 * The messages sent to /llm/chat for a long conversation: system messages are kept, then the most
 * recent messages that fit in `maxChars` / `maxMessages`. The last message is always sent, and the
 * kept part never starts with an assistant turn (some providers require a user turn first).
 */
export function trimHistory(messages: ChatMessage[], maxChars = HISTORY_CHAR_BUDGET, maxMessages = HISTORY_MAX_MESSAGES): ChatMessage[] {
  const system = messages.filter((m) => m.role === 'system');
  const rest = messages.filter((m) => m.role !== 'system');
  let budget = maxChars - system.reduce((n, m) => n + (m.content?.length ?? 0), 0);
  const kept: ChatMessage[] = [];
  for (let i = rest.length - 1; i >= 0; i--) {
    const size = rest[i].content?.length ?? 0;
    if (kept.length > 0 && (size > budget || kept.length >= maxMessages)) break;
    kept.unshift(rest[i]);
    budget -= size;
  }
  while (kept.length > 1 && kept[0].role === 'assistant') kept.shift();
  return [...system, ...kept];
}

export interface ChatGroup<T> {
  label: string;
  chats: T[];
}

/** Pinned first, then Today / Yesterday / This week (the last 7 days) / Older, newest first, in `tz`. */
export function groupChatsByDate<T extends { updated_at: string; pinned?: boolean }>(chats: T[], now: Date = new Date(), tz?: string): ChatGroup<T>[] {
  const today = isoDay(now, tz);
  const yesterday = addDays(today, -1);
  const weekStart = addDays(today, -6);
  const order = ['Pinned', 'Today', 'Yesterday', 'This week', 'Older'];
  const buckets = new Map<string, T[]>(order.map((l) => [l, []]));
  const sorted = [...chats].sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''));
  for (const c of sorted) {
    let label: string;
    if (c.pinned) label = 'Pinned';
    else {
      const t = new Date(c.updated_at);
      const day = Number.isNaN(t.getTime()) ? '' : isoDay(t, tz);
      label = day >= today ? 'Today' : day === yesterday ? 'Yesterday' : day >= weekStart ? 'This week' : 'Older';
    }
    buckets.get(label)!.push(c);
  }
  return order.filter((l) => buckets.get(l)!.length).map((label) => ({ label, chats: buckets.get(label)! }));
}

export function matchesChatQuery(chat: { title: string; preview?: string }, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return chat.title.toLowerCase().includes(needle) || (chat.preview ?? '').toLowerCase().includes(needle);
}

// ------------------------------------------------------------------ local queue

/** A conversation as the PWA holds it; `synced` is false while the hub has not accepted the latest version. */
export interface LocalChat {
  id: string;
  title: string;
  title_source: ChatSummary['title_source'];
  model: string | null;
  messages: ChatThreadMessage[];
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export function loadPending(): Record<string, LocalChat> {
  const raw = readPref<Record<string, LocalChat>>(PENDING_KEY, {});
  return raw && typeof raw === 'object' ? raw : {};
}

export function putPending(chat: LocalChat): void {
  writePref(PENDING_KEY, { ...loadPending(), [chat.id]: chat });
}

export function dropPending(id: string): void {
  const all = loadPending();
  if (!(id in all)) return;
  delete all[id];
  writePref(PENDING_KEY, all);
}

export function summaryOf(chat: LocalChat): ChatSummary {
  const last = [...chat.messages].reverse().find((m) => m.content.trim());
  return {
    id: chat.id,
    title: chat.title,
    title_source: chat.title_source,
    model: chat.model,
    message_count: chat.messages.length,
    preview: last ? last.content.replace(/\s+/g, ' ').trim().slice(0, 140) : '',
    pinned: chat.pinned,
    created_at: chat.created_at,
    updated_at: chat.updated_at,
  };
}

export function localFromThread(t: ChatThread): LocalChat {
  return {
    id: t.id,
    title: t.title,
    title_source: t.title_source,
    model: t.model,
    messages: t.messages,
    pinned: t.pinned,
    created_at: t.created_at,
    updated_at: t.updated_at,
  };
}

/**
 * Worth retrying later: the hub was unreachable, timed out, rate limited or failed on its side, or
 * does not have `/chats` yet (404/405 from a hub deployed before this PWA; a PUT never 404s otherwise).
 */
export function isRetryable(err: unknown): boolean {
  if (!(err instanceof HubError)) return true;
  return [0, 404, 405, 408, 429].includes(err.status) || err.status >= 500;
}

/** `PUT` one conversation and take it off the queue; a non-retryable refusal also clears it (it would never pass). */
export async function saveChat(client: HubClient, chat: LocalChat): Promise<ChatThread> {
  putPending(chat);
  try {
    const saved = await client.chatSave(chat.id, { model: chat.model, messages: chat.messages });
    const current = loadPending()[chat.id];
    // A newer version queued while this request was in flight stays queued.
    if (current && current.updated_at === chat.updated_at) dropPending(chat.id);
    return saved;
  } catch (err) {
    if (!isRetryable(err)) dropPending(chat.id);
    throw err;
  }
}

/** Retries every queued conversation; returns how many are still waiting. */
export async function syncPending(client: HubClient): Promise<number> {
  for (const chat of Object.values(loadPending())) {
    try {
      await saveChat(client, chat);
    } catch {
      // Still queued when retryable; saveChat dropped it otherwise.
    }
  }
  return Object.keys(loadPending()).length;
}
