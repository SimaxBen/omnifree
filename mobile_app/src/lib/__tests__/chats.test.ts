import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentUnavailable } from '../agent';
import { HubError, type HubClient } from '../api';
import {
  chatTitleFrom,
  groupChatsByDate,
  isRetryable,
  loadPending,
  matchesChatQuery,
  newChatId,
  saveChat,
  summaryOf,
  syncPending,
  trimHistory,
  type LocalChat,
} from '../chats';
import type { ChatMessage, ChatThread } from '../types';

const msg = (role: ChatMessage['role'], content: string): ChatMessage => ({ role, content });

describe('trimHistory', () => {
  it('keeps everything when it fits', () => {
    const all = [msg('system', 'be brief'), msg('user', 'a'), msg('assistant', 'b'), msg('user', 'c')];
    expect(trimHistory(all, 1000)).toEqual(all);
  });

  it('drops the oldest turns past the budget, keeping system messages and the last message', () => {
    const all = [msg('system', 'sys'), msg('user', 'x'.repeat(50)), msg('assistant', 'y'.repeat(50)), msg('user', 'z'.repeat(30)), msg('assistant', 'w'.repeat(30)), msg('user', 'last')];
    const out = trimHistory(all, 3 + 30 + 30 + 4);
    expect(out.map((m) => m.content?.[0])).toEqual(['s', 'z', 'w', 'l']);
  });

  it('never starts the kept part with an assistant turn', () => {
    const all = [msg('user', 'u'.repeat(40)), msg('assistant', 'a'.repeat(10)), msg('user', 'q')];
    expect(trimHistory(all, 15)).toEqual([msg('user', 'q')]);
  });

  it('always sends the last message, even alone over budget, and caps the count', () => {
    expect(trimHistory([msg('user', 'old'), msg('user', 'x'.repeat(500))], 100)).toEqual([msg('user', 'x'.repeat(500))]);
    const many = Array.from({ length: 10 }, (_, i) => msg(i % 2 ? 'assistant' : 'user', String(i)));
    expect(trimHistory(many, 10_000, 4).map((m) => m.content)).toEqual(['6', '7', '8', '9']);
  });
});

describe('groupChatsByDate', () => {
  const now = new Date('2026-10-21T15:00:00Z');
  const c = (id: string, updated_at: string, pinned = false) => ({ id, updated_at, pinned });

  it('groups pinned, today, yesterday, this week and older, newest first', () => {
    const groups = groupChatsByDate(
      [
        c('old', '2026-09-01T10:00:00Z'),
        c('today-early', '2026-10-21T01:00:00Z'),
        c('yday', '2026-10-20T12:00:00Z'),
        c('pinned-old', '2026-01-01T00:00:00Z', true),
        c('week', '2026-10-16T12:00:00Z'),
        c('today-late', '2026-10-21T14:00:00Z'),
        c('eight-days', '2026-10-13T12:00:00Z'),
      ],
      now,
      'UTC',
    );
    expect(groups.map((g) => [g.label, g.chats.map((x) => x.id)])).toEqual([
      ['Pinned', ['pinned-old']],
      ['Today', ['today-late', 'today-early']],
      ['Yesterday', ['yday']],
      ['This week', ['week']],
      ['Older', ['eight-days', 'old']],
    ]);
  });

  it('uses the given time zone for day boundaries and skips empty groups', () => {
    // 23:30 UTC on Oct 20 is already Oct 21 in Casablanca (UTC+1).
    const groups = groupChatsByDate([c('late', '2026-10-20T23:30:00Z')], now, 'Africa/Casablanca');
    expect(groups).toEqual([{ label: 'Today', chats: [c('late', '2026-10-20T23:30:00Z')] }]);
    expect(groupChatsByDate([], now)).toEqual([]);
  });
});

describe('titles, ids and search', () => {
  it('derives the title from the first user message', () => {
    expect(chatTitleFrom([msg('system', 's'), msg('user', '  How do I\n\n *bake* bread? ')])).toBe('How do I bake bread?');
    expect(chatTitleFrom([msg('assistant', 'hi')])).toBe('New chat');
    const long = chatTitleFrom([msg('user', 'word '.repeat(40))]);
    expect(long).toHaveLength(60);
    expect(long.endsWith('…')).toBe(true);
  });

  it('makes ids the hub accepts', () => {
    const id = newChatId();
    expect(id).toMatch(/^[A-Za-z0-9_-]{6,64}$/);
    expect(newChatId()).not.toBe(id);
  });

  it('searches title and preview, case-insensitively', () => {
    expect(matchesChatQuery({ title: 'Trip to Fes', preview: 'Day 1' }, 'fes')).toBe(true);
    expect(matchesChatQuery({ title: 'Trip', preview: 'Day 1' }, 'DAY')).toBe(true);
    expect(matchesChatQuery({ title: 'Trip', preview: '' }, 'x')).toBe(false);
    expect(matchesChatQuery({ title: 'Trip' }, '  ')).toBe(true);
  });
});

describe('local queue', () => {
  const store = new Map<string, string>();
  vi.stubGlobal('window', {
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    },
  });
  afterEach(() => store.clear());

  const chat: LocalChat = {
    id: 'cabc123',
    title: 'Hello',
    title_source: 'auto',
    model: null,
    messages: [
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: 'Hi   there' },
    ],
    pinned: false,
    created_at: '2026-10-21T10:00:00Z',
    updated_at: '2026-10-21T10:01:00Z',
  };
  const thread = { ...summaryOf(chat), messages: chat.messages } as ChatThread;

  it('summarises a local conversation', () => {
    expect(summaryOf(chat)).toMatchObject({ id: 'cabc123', message_count: 2, preview: 'Hi there' });
  });

  it('clears a saved conversation and keeps a failed one for later', async () => {
    const ok = { chatSave: vi.fn().mockResolvedValue(thread) } as unknown as HubClient;
    await saveChat(ok, chat);
    expect(loadPending()).toEqual({});

    const offline = { chatSave: vi.fn().mockRejectedValue(new HubError('Cannot reach hub', 0)) } as unknown as HubClient;
    await expect(saveChat(offline, chat)).rejects.toThrow('Cannot reach hub');
    expect(Object.keys(loadPending())).toEqual(['cabc123']);

    expect(await syncPending(offline)).toBe(1);
    expect(await syncPending(ok)).toBe(0);
  });

  it('drops a conversation the hub refuses for good', async () => {
    const tooBig = { chatSave: vi.fn().mockRejectedValue(new HubError('conversation too long', 413)) } as unknown as HubClient;
    await expect(saveChat(tooBig, chat)).rejects.toThrow();
    expect(loadPending()).toEqual({});
  });

  it('classifies retryable errors', () => {
    expect(isRetryable(new HubError('x', 0))).toBe(true);
    expect(isRetryable(new HubError('x', 503))).toBe(true);
    expect(isRetryable(new HubError('x', 429))).toBe(true);
    expect(isRetryable(new HubError('x', 404))).toBe(true);
    expect(isRetryable(new HubError('x', 422))).toBe(false);
    expect(isRetryable(new HubError('x', 413))).toBe(false);
    expect(isRetryable(new TypeError('network'))).toBe(true);
  });
});

describe('agentUnavailable', () => {
  it('recognises the hub answers for a missing or down Instinct server', () => {
    expect(agentUnavailable(new HubError('instinct_url not configured', 503))).toEqual({ reason: 'not_configured', detail: 'instinct_url not configured' });
    expect(agentUnavailable(new HubError('instinct unreachable: ConnectError()', 502))?.reason).toBe('unreachable');
    expect(agentUnavailable(new HubError('instinct timeout: ReadTimeout()', 504))?.reason).toBe('unreachable');
    expect(agentUnavailable(new HubError('hub token not configured', 503))).toBeNull();
    expect(agentUnavailable(new HubError('instinct says no', 400))).toBeNull();
    expect(agentUnavailable(new Error('instinct'))).toBeNull();
  });
});
