import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { Markdown } from '../../components/Markdown';
import type { HubClient } from '../api';
import { completeWikiLink, parseBlocks, parseInline, plainText, wikiLinks, wikiQuery } from '../markdown';
import {
  clearPendingMeeting,
  finishMeeting,
  loadPendingMeeting,
  meetingPartKey,
  meetingProgress,
  meetingStatusText,
  newSession,
  pickMime,
  rememberPart,
  savePendingMeeting,
  type MeetingStatus,
} from '../meeting';
import { normalizeTag, notesByTag, onTagFilter, requestTagFilter, takePendingTag } from '../noteLinks';
import { canBook, currentBlockIndex, durationLabel, minutesOf, normalizePlan, planStats, visibleWindow, type PlanBlock } from '../plan';

const html = (text: string, props: Partial<Parameters<typeof Markdown>[0]> = {}) => renderToStaticMarkup(createElement(Markdown, { text, ...props }));

describe('wiki links and tags in the Markdown parser', () => {
  it('parses [[links]] with labels and sections', () => {
    expect(parseInline('see [[Project  X]] and [[Plan#Goals|the plan]]!')).toEqual([
      { t: 'text', v: 'see ' },
      { t: 'wikilink', title: 'Project X', label: 'Project X' },
      { t: 'text', v: ' and ' },
      { t: 'wikilink', title: 'Plan', label: 'the plan' },
      { t: 'text', v: '!' },
    ]);
    expect(parseInline('[[ ]] and `[[code]]`')).toEqual([{ t: 'text', v: '[[ ]] and ' }, { t: 'code', v: '[[code]]' }]);
    expect(parseInline('\\[[not]]')[0]).toEqual({ t: 'text', v: '[[not]]' });
  });

  it('parses #tags only at a word start, with a letter first', () => {
    expect(parseInline('#idea and #Projet-x- but C#, #123, a#b')).toEqual([
      { t: 'tag', tag: 'idea' },
      { t: 'text', v: ' and ' },
      { t: 'tag', tag: 'projet-x' },
      { t: 'text', v: '- but C#, #123, a#b' },
    ]);
    expect(parseInline('(#été)')).toEqual([{ t: 'text', v: '(' }, { t: 'tag', tag: 'été' }, { t: 'text', v: ')' }]);
    expect(parseBlocks('# Heading')[0]).toEqual({ t: 'heading', level: 1, text: 'Heading' });
  });

  it('parses :::details sections, nested ones included', () => {
    const blocks = parseBlocks('Intro\n:::details Full transcript\nline **one**\n\n:::details inner\nx\n:::\n:::\nAfter');
    expect(blocks[0]).toEqual({ t: 'para', text: 'Intro' });
    expect(blocks[1]).toMatchObject({ t: 'details', summary: 'Full transcript' });
    const inner = (blocks[1] as { blocks: unknown[] }).blocks;
    expect(inner[0]).toEqual({ t: 'para', text: 'line **one**' });
    expect(inner[1]).toMatchObject({ t: 'details', summary: 'inner' });
    expect(blocks[2]).toEqual({ t: 'para', text: 'After' });
    expect(parseBlocks(':::details\nopen to the end')[0]).toMatchObject({ t: 'details', summary: 'Details' });
  });

  it('keeps previews clean', () => {
    expect(plainText('See [[Project X]] and [[Plan|the plan]].\n:::details Transcript\nhello\n:::')).toBe('See Project X and the plan. hello');
  });

  it('lists linked titles like the hub, code ignored', () => {
    expect(wikiLinks('[[A]] [[a]] [[B|label]] `[[C]]`\n```\n[[D]]\n```\n[[ E  f ]]')).toEqual(['A', 'B', 'E f']);
  });

  it('finds the [[ being typed and completes it', () => {
    expect(wikiQuery('text [[Pro', 10)).toEqual({ start: 5, query: 'Pro' });
    expect(wikiQuery('text [[Pro]] more', 17)).toBeNull();
    expect(wikiQuery('[[a\nb', 5)).toBeNull();
    expect(wikiQuery('no link', 7)).toBeNull();
    expect(completeWikiLink('see [[Pro and', 4, 9, 'Project X')).toEqual({ text: 'see [[Project X]] and', caret: 17 });
    // Already closed: the rest of the old title and its ]] are replaced, not doubled.
    expect(completeWikiLink('[[Pro]] end', 0, 5, 'Project|X')).toEqual({ text: '[[Project X]] end', caret: 13 });
  });
});

describe('<Markdown> links, tags and details', () => {
  it('renders tappable links and tags only with handlers, never raw HTML', () => {
    const plain = html('[[Note <b>x</b>]] #tag');
    expect(plain).toContain('<span class="break-words font-medium text-signal">Note &lt;b&gt;x&lt;/b&gt;</span>');
    expect(plain).not.toContain('<button');
    const live = html('[[Note]] #tag', { onWikiLink: () => undefined, onTag: () => undefined });
    expect(live).toMatch(/<button type="button"[^>]*>Note<\/button>/);
    expect(live).toContain('aria-label="Notes tagged tag"');
  });

  it('never nests buttons inside a link', () => {
    const out = html('[read #tag](https://x.test)', { onTag: () => undefined });
    expect(out).toContain('<a href="https://x.test"');
    expect(out).not.toContain('<button');
  });

  it('renders details collapsed', () => {
    const out = html(':::details Full transcript\nhello\n:::');
    expect(out).toMatch(/<details[^>]*><summary[^>]*>.*Full transcript.*<\/summary><div[^>]*><p[^>]*>hello<\/p><\/div><\/details>/);
    expect(out).not.toContain(' open');
  });
});

describe('plan helpers', () => {
  const blocks: PlanBlock[] = [
    { start: '08:00', end: '09:30', kind: 'task', ref_id: 't1', title: 'Report', reason: 'due today', fixed: false },
    { start: '09:30', end: '09:45', kind: 'break', ref_id: null, title: 'Break', reason: '', fixed: false },
    { start: '10:00', end: '11:00', kind: 'event', ref_id: 'e1', title: 'Standup', reason: 'Calendar', fixed: true },
    { start: '15:00', end: '15:30', kind: 'task', ref_id: 't2', title: 'Dentist', reason: 'due at 15:00', fixed: true },
  ];

  it('normalizes hub answers safely', () => {
    expect(normalizePlan(null)).toMatchObject({ status: 'none', blocks: [], booked: 0, source: null });
    const p = normalizePlan({ day: '2030-03-12', status: 'accepted', source: 'llm', blocks: [...blocks, { start: 1 }], booked: 2, calendar: { configured: true, writable: true } });
    expect(p.blocks).toHaveLength(4);
    expect(p.status).toBe('accepted');
    expect(canBook(p)).toBe(true);
    expect(canBook({ ...p, calendar: { configured: true, writable: false } })).toBe(false);
    expect(canBook({ ...p, blocks: blocks.filter((b) => b.fixed) })).toBe(false);
    expect(p.agenda_error).toBeNull();
    expect(p.cleared_previous).toBeUndefined();
    const q = normalizePlan({ agenda_error: 'timed out', cleared_previous: false });
    expect([q.agenda_error, q.cleared_previous]).toEqual(['timed out', false]);
  });

  it('labels durations and counts focus time', () => {
    expect(minutesOf('09:30')).toBe(570);
    expect(durationLabel(blocks[0])).toBe('1 h 30');
    expect(durationLabel(blocks[1])).toBe('15 min');
    expect(durationLabel(blocks[2])).toBe('1 h');
    expect(planStats(normalizePlan({ blocks }))).toEqual({ tasks: 2, focusMinutes: 120, events: 1, done: 0 });
    const ticked = blocks.map((b, i) => (b.kind === 'task' && i === blocks.findIndex((x) => x.kind === 'task') ? { ...b, done: true } : b));
    expect(planStats(normalizePlan({ blocks: ticked })).done).toBe(1);
  });

  it('finds the current or next block', () => {
    expect(currentBlockIndex(blocks, '08:10')).toBe(0);
    expect(currentBlockIndex(blocks, '09:50')).toBe(2);
    expect(currentBlockIndex(blocks, '07:00')).toBe(0);
    expect(currentBlockIndex(blocks, '16:00')).toBe(-1);
  });

  it('centers the collapsed timeline on the live block', () => {
    const day: PlanBlock[] = Array.from({ length: 10 }, (_, i) => ({
      start: `${String(8 + i).padStart(2, '0')}:00`, end: `${String(8 + i).padStart(2, '0')}:45`, kind: 'task', ref_id: `t${i}`, title: `T${i}`, reason: '', fixed: false,
    }));
    expect(visibleWindow(day, '07:00', 6)).toEqual({ start: 0, end: 6, current: 0 });
    expect(visibleWindow(day, '08:10', 6)).toEqual({ start: 0, end: 6, current: 0 });
    expect(visibleWindow(day, '13:10', 6)).toEqual({ start: 4, end: 10, current: 5 }); // one finished block kept
    expect(visibleWindow(day, '12:10', 6)).toEqual({ start: 3, end: 9, current: 4 });
    expect(visibleWindow(day, '17:50', 6)).toEqual({ start: 4, end: 10, current: -1 }); // day over: the last ones
    expect(visibleWindow(blocks, '15:10', 6)).toEqual({ start: 0, end: 4, current: 3 });
    expect(visibleWindow(day, '13:10', 10)).toEqual({ start: 0, end: 10, current: 5 }); // expanded
  });
});

describe('meeting helpers', () => {
  it('names parts and picks a recorder format', () => {
    expect(meetingPartKey('abc', 7, 'webm', '2026-10-20')).toBe('meetings/2026/10/abc/part-007.webm');
    expect(meetingPartKey('abc', 12, '.m4a', '2026-01-02')).toBe('meetings/2026/01/abc/part-012.m4a');
    expect(newSession(1_700_000_000_000, () => 0)).toMatch(/^[0-9a-z]+0000$/);
    expect(pickMime((t) => t === 'audio/webm')).toBe('audio/webm');
    expect(pickMime(() => false)).toBeUndefined();
    expect(pickMime(() => {
      throw new Error('no');
    })).toBeUndefined();
  });

  it('reports progress and status', () => {
    expect(meetingProgress(null)).toBe(0);
    expect(meetingProgress({ status: 'transcribing', parts_total: 4, parts_done: 2 })).toBeCloseTo(0.45);
    expect(meetingProgress({ status: 'summarizing', parts_total: 4, parts_done: 4 })).toBe(0.95);
    expect(meetingProgress({ status: 'done', parts_total: 4, parts_done: 4 })).toBe(1);
    expect(meetingStatusText({ status: 'transcribing', parts_total: 3, parts_done: 1 })).toBe('Transcribing part 2 of 3');
    expect(meetingStatusText({ status: 'summarizing', parts_total: 3, parts_done: 3 })).toBe('Writing the summary and action items');
  });

  it('calls the hub until the note is written, waiting while busy', async () => {
    const base = { meeting_id: 'm1', final: true, parts_total: 2, parts_failed: 0, note_id: null, task_ids: [], error: null };
    const answers: Partial<MeetingStatus>[] = [
      { status: 'transcribing', parts_done: 1, remaining: 1 },
      { status: 'transcribing', parts_done: 1, remaining: 1, busy: true },
      { status: 'done', parts_done: 2, remaining: 0, note_id: 'n1' },
    ];
    const request = vi.fn(async () => ({ ...base, ...answers.shift() }));
    const client = { request } as unknown as HubClient;
    const seen: string[] = [];
    const wait = vi.fn(async () => undefined);
    const done = await finishMeeting(client, 'm1', (s) => seen.push(s.status), undefined, wait);
    expect(done.note_id).toBe('n1');
    expect(seen).toEqual(['transcribing', 'transcribing', 'done']);
    expect(wait).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('POST', '/notes/meeting', { json: { meeting_id: 'm1', final: true }, signal: undefined });
    await expect(finishMeeting({ request: vi.fn(async () => ({ ...base, status: 'transcribing', parts_done: 0 })) } as unknown as HubClient, 'm1', () => undefined, undefined, wait, 2)).rejects.toThrow(/still being processed/);
  });
});

describe('note links client', () => {
  it('normalizes tags and hands a tapped tag to the notes list', () => {
    expect(normalizeTag('  ##Idea ')).toBe('idea');
    const got: string[] = [];
    const off = onTagFilter((t) => got.push(t));
    requestTagFilter('#Work');
    expect(got).toEqual(['work']);
    expect(takePendingTag()).toBe('work');
    expect(takePendingTag()).toBeNull();
    off();
    requestTagFilter('later');
    expect(got).toEqual(['work']);
    expect(takePendingTag()).toBe('later');
  });

  it('asks the hub for the notes of a tag', async () => {
    const request = vi.fn(async () => ({ notes: [{ id: 'n1' }], notebooks: [{ name: 'Inbox', count: 1 }] }));
    const res = await notesByTag({ request } as unknown as HubClient, '#Idea', { limit: 10 });
    expect(request).toHaveBeenCalledWith('GET', '/notes', { query: { tag: 'idea', notebook: undefined, limit: 10 }, signal: undefined });
    expect(res.notes).toHaveLength(1);
    expect(res.notebooks[0].name).toBe('Inbox');
  });
});

describe('unfinished meetings', () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
  };

  it('remembers every uploaded part and the meeting id until the note exists', () => {
    const kv = mem();
    expect(loadPendingMeeting(kv)).toBeNull();
    let p = rememberPart(null, 's1', { key: 'meetings/a/part-000.webm', seconds: 600 }, null, 'Weekly', 1000);
    p = rememberPart(p, 's1', { key: 'meetings/a/part-000.webm', seconds: 600 }, 'm1');
    p = rememberPart(p, 's1', { key: 'meetings/a/part-001.webm', seconds: 30 }, null);
    savePendingMeeting(p, kv);
    expect(loadPendingMeeting(kv)).toEqual({ meetingId: 'm1', session: 's1', title: 'Weekly', startedAt: 1000, parts: [{ key: 'meetings/a/part-000.webm', seconds: 600 }, { key: 'meetings/a/part-001.webm', seconds: 30 }] });
    expect(rememberPart(p, 's2', null, null, undefined, 5).parts).toEqual([]); // a new meeting starts over
    kv.setItem('omnifree.meeting.pending', '{broken');
    expect(loadPendingMeeting(kv)).toBeNull();
    savePendingMeeting(p, kv);
    clearPendingMeeting(kv);
    expect(loadPendingMeeting(kv)).toBeNull();
    expect(loadPendingMeeting(null)).toBeNull();
  });

  it('sends every part with the first final call, and creates the meeting when it has no id yet', async () => {
    const base = { final: true, parts_total: 2, parts_failed: 0, note_id: null, task_ids: [], error: null };
    const answers: Partial<MeetingStatus>[] = [
      { meeting_id: 'm9', status: 'transcribing', parts_done: 1, remaining: 1 },
      { meeting_id: 'm9', status: 'done', parts_done: 2, remaining: 0, note_id: 'n1' },
    ];
    const request = vi.fn(async () => ({ ...base, ...answers.shift() }));
    const parts = [{ key: 'meetings/a/part-000.webm' }, { key: 'meetings/a/part-001.webm' }];
    const done = await finishMeeting({ request } as unknown as HubClient, null, () => undefined, undefined, async () => undefined, 5, parts);
    expect(done.note_id).toBe('n1');
    expect(request.mock.calls[0]).toEqual(['POST', '/notes/meeting', { json: { audio_keys: parts, final: true }, signal: undefined }]);
    expect(request.mock.calls[1]).toEqual(['POST', '/notes/meeting', { json: { meeting_id: 'm9', final: true }, signal: undefined }]);
  });
});

describe('tag filter with a search', () => {
  it('passes the search words along with the tag', async () => {
    const request = vi.fn(async () => ({ notes: [], notebooks: [] }));
    await notesByTag({ request } as unknown as HubClient, 'idea', { limit: 5, q: '  garden ' });
    expect(request).toHaveBeenCalledWith('GET', '/notes', { query: { tag: 'idea', notebook: undefined, limit: 5, q: 'garden' }, signal: undefined });
  });
});
