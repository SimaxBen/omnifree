import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubClient } from '../api';
import {
  assistantChat,
  assistantConfirm,
  assistantRoute,
  attachmentKey,
  attachmentKind,
  busyLine,
  chipState,
  detectLanguage,
  failureLine,
  generalHistory,
  isSpokenNo,
  isSpokenYes,
  memoryCreate,
  memoryDelete,
  memoryForgetAll,
  memoryList,
  memoryUpdate,
  pickVoice,
  preferenceSystem,
  savedTools,
  sendPath,
  speakableText,
  toolLabel,
  usesAssistant,
} from '../assistant';

describe('assistant pure helpers', () => {
  it('routes through the assistant unless the data mode is Never and nothing is attached', () => {
    expect(usesAssistant('auto', 0)).toBe(true);
    expect(usesAssistant('always', 0)).toBe(true);
    expect(usesAssistant('never', 0)).toBe(false);
    expect(usesAssistant('never', 1)).toBe(true);
  });

  it('streams general questions: plain in Never, a keyword probe in Auto when streaming, else the assistant', () => {
    expect(sendPath('never', 0, true)).toBe('plain');
    expect(sendPath('auto', 0, true)).toBe('probe');
    expect(sendPath('auto', 0, false)).toBe('assistant');
    expect(sendPath('auto', 1, true)).toBe('assistant');
    expect(sendPath('always', 0, true)).toBe('assistant');
    expect(sendPath('never', 2, true)).toBe('assistant');
  });

  it('withholds earlier data answers from a general (plain) chat', () => {
    const out = generalHistory([
      { role: 'user', content: 'combien j ai dépensé ?' },
      { role: 'assistant', content: '3 200 MAD', data: true },
      { role: 'assistant', content: 'Bonjour' },
    ]);
    expect(out[1].content).toMatch(/withheld/);
    expect(out[2]).toEqual({ role: 'assistant', content: 'Bonjour' });
    expect(JSON.stringify(out)).not.toMatch(/3 200|"data"/);
  });

  it('turns answer preferences into one system message', () => {
    expect(preferenceSystem([])).toBeNull();
    expect(preferenceSystem([' ', ''])).toBeNull();
    expect(preferenceSystem(['Je préfère le darija', 'short answers'])).toBe(
      'How the owner wants answers (saved preferences, follow them):\n- Je préfère le darija\n- short answers',
    );
  });

  it('shows proposed writes as proposed until confirmed, and saves their final status', () => {
    const read = { name: 'tasks_list', summary: '2 tasks', ok: true };
    const failed = { name: 'expenses_month', summary: 'error: x', ok: false };
    const proposed = { name: 'expense_add', summary: 'Expense 350 MAD', ok: true, pending: true, action_id: 'a1' };
    expect(chipState(read)).toBe('ok');
    expect(chipState(failed)).toBe('error');
    expect(chipState(proposed)).toBe('proposed');
    expect(chipState(proposed, [{ id: 'a1', status: 'waiting' }])).toBe('proposed');
    expect(chipState(proposed, [{ id: 'a1', status: 'done' }])).toBe('created');
    expect(chipState(proposed, [{ id: 'a1', status: 'dismissed' }])).toBe('dismissed');
    expect(chipState({ name: 'expense_add', summary: 'x', status: 'not-created' })).toBe('not-created');
    expect(savedTools([read, proposed], [{ id: 'a1', status: 'done' }])).toEqual([
      { name: 'tasks_list', summary: '2 tasks', ok: true },
      { name: 'expense_add', summary: 'Expense 350 MAD', ok: true, status: 'created' },
    ]);
    expect(savedTools([proposed], [{ id: 'a1', status: 'error' }])[0].status).toBe('not-created');
    expect(savedTools([proposed])[0].status).toBe('not-created');
    expect(savedTools([{ name: 'expense_add', summary: 'x', status: 'dismissed' }])[0].status).toBe('dismissed');
  });

  it('has a spoken failure and busy line per language', () => {
    expect(failureLine('fr')).toMatch(/^Désolé/);
    expect(failureLine('en')).toMatch(/^Sorry/);
    expect(failureLine('ar').length).toBeGreaterThan(5);
    expect(busyLine('fr')).toMatch(/^Un instant/);
    expect(busyLine('en')).toMatch(/^One moment/);
  });

  it('builds chat/YYYY/MM attachment keys with a timestamp and a safe name', () => {
    const at = new Date(2026, 9, 5, 7, 3, 9);
    expect(attachmentKey('Facture Lydec (oct).PDF', at)).toBe('chat/2026/10/20261005-070309-Facture-Lydec-oct.pdf');
    expect(attachmentKey('été à Fès.jpeg', at)).toBe('chat/2026/10/20261005-070309-ete-a-Fes.jpeg');
    expect(attachmentKey('???', at)).toBe('chat/2026/10/20261005-070309-file');
    expect(attachmentKey('.env', at)).toBe('chat/2026/10/20261005-070309-env');
  });

  it('accepts images, PDFs and text files within their size limits', () => {
    expect(attachmentKind('a.jpg', 'image/jpeg', 1000)).toEqual({ kind: 'image' });
    expect(attachmentKind('scan.PDF', '', 1000)).toEqual({ kind: 'pdf' });
    expect(attachmentKind('notes.md', '', 1000)).toEqual({ kind: 'text' });
    expect(attachmentKind('data.json', 'application/json', 10)).toEqual({ kind: 'text' });
    expect(attachmentKind('song.mp3', 'audio/mpeg', 10)).toHaveProperty('error');
    expect(attachmentKind('big.png', 'image/png', 9 * 1024 * 1024)).toEqual({ error: 'big.png is too large (max 8 MB for images)' });
    expect(attachmentKind('big.pdf', 'application/pdf', 21 * 1024 * 1024)).toEqual({ error: 'big.pdf is too large (max 20 MB for PDFs)' });
  });

  it('detects the language to speak like the hub does', () => {
    expect(detectLanguage('Tu as dépensé 120 MAD')).toBe('fr');
    expect(detectLanguage('You spent 120 MAD this month')).toBe('en');
    expect(detectLanguage('صرفت 120 درهم هاد الشهر')).toBe('ar');
    expect(detectLanguage('wach 3ndi chi rendez-vous')).toBe('fr');
    expect(detectLanguage('')).toBe('en');
  });

  it('picks the best installed voice', () => {
    const voices = [
      { lang: 'en-GB', name: 'UK' },
      { lang: 'fr_CA', name: 'Canada', localService: true },
      { lang: 'fr-FR', name: 'France' },
      { lang: 'ar-SA', name: 'Saudi' },
    ];
    expect(pickVoice(voices, 'fr')?.name).toBe('France');
    expect(pickVoice(voices, 'ar')?.name).toBe('Saudi');
    expect(pickVoice(voices, 'en')?.name).toBe('UK');
    expect(pickVoice([{ lang: 'de-DE', name: 'DE' }], 'fr')).toBeNull();
  });

  it('strips markdown before speaking and stops at a sentence end', () => {
    expect(speakableText('## Total\n- **120 MAD** chez [Marjane](https://x.y)\n```js\ncode()\n```\n| a | b |')).toBe('Total 120 MAD chez Marjane');
    const long = `${'Phrase courte. '.repeat(20)}fin`;
    const out = speakableText(long, 100);
    expect(out.length).toBeLessThanOrEqual(100);
    expect(out.endsWith('.')).toBe(true);
  });

  it('understands spoken confirmations', () => {
    expect(isSpokenYes('Oui vas-y')).toBe(true);
    expect(isSpokenYes('wakha')).toBe(true);
    expect(isSpokenYes('Confirme.')).toBe(true);
    expect(isSpokenYes('ajoute une tâche')).toBe(false);
    expect(isSpokenNo('non merci')).toBe(true);
    expect(isSpokenNo('annule')).toBe(true);
    expect(isSpokenNo('note ça')).toBe(false);
    // Arabic script (Whisper writes Darija this way) and short French words that are not answers
    expect(isSpokenYes('نعم')).toBe(true);
    expect(isSpokenYes('واخا')).toBe(true);
    expect(isSpokenYes('واخا، زيدها')).toBe(true);
    expect(isSpokenNo('لا')).toBe(true);
    expect(isSpokenNo('لا شكرا')).toBe(true);
    expect(isSpokenYes('لا')).toBe(false);
    expect(isSpokenNo("la facture d'eau ?")).toBe(false);
    expect(isSpokenNo('la')).toBe(true);
    expect(isSpokenNo('Lla.')).toBe(true);
    expect(isSpokenYes('ah bon ?')).toBe(false);
    expect(isSpokenYes('Ah !')).toBe(true);
    expect(isSpokenYes('ok non attends')).toBe(false);
    expect(isSpokenYes('okapi')).toBe(false);
  });

  it('labels tools for the chips', () => {
    expect(toolLabel('expenses_month')).toBe('Expenses');
    expect(toolLabel('remember')).toBe('Memory');
    expect(toolLabel('some_new_tool')).toBe('some new tool');
  });
});

interface Call {
  method: string;
  url: URL;
  json?: unknown;
}

function mockHub(reply: (call: Call) => { status?: number; body?: unknown }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      const call: Call = { method: init.method ?? 'GET', url: new URL(url), json: typeof init.body === 'string' ? JSON.parse(init.body) : undefined };
      calls.push(call);
      const { status = 200, body } = reply(call);
      if (status === 204) return new Response(null, { status });
      return new Response(JSON.stringify(body ?? {}), { status, headers: { 'content-type': 'application/json' } });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });

describe('assistant hub calls', () => {
  it('posts a chat turn and a confirmation', async () => {
    const calls = mockHub((c) => ({ body: c.url.pathname.endsWith('/confirm') ? { ok: true, tool: 'tasks_add', summary: 'Task "x"', result: {} } : { content: 'hi', routed: 'general' } }));
    const reply = await assistantChat(client, { messages: [{ role: 'user', content: 'salut' }], mode: 'auto', attachments: [] });
    expect(reply.content).toBe('hi');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url.pathname).toBe('/api/assistant/chat');
    expect(calls[0].json).toEqual({ messages: [{ role: 'user', content: 'salut' }], mode: 'auto', attachments: [] });
    const done = await assistantConfirm(client, { tool: 'tasks_add', args: { title: 'x' } });
    expect(done.ok).toBe(true);
    expect(calls[1].json).toEqual({ tool: 'tasks_add', args: { title: 'x' } });
  });

  it('asks the keyword router', async () => {
    const calls = mockHub(() => ({ body: { route: 'general', reason: 'general-keyword', preferences: ['x'] } }));
    const r = await assistantRoute(client, { text: 'translate hi', previous_route: 'data', mode: 'auto' });
    expect(r.route).toBe('general');
    expect(calls[0].url.pathname).toBe('/api/assistant/route');
    expect(calls[0].json).toEqual({ text: 'translate hi', previous_route: 'data', mode: 'auto' });
  });

  it('lists, creates, updates, deletes and forgets memories', async () => {
    const fact = { id: 'm1', fact: 'Mon salaire tombe le 25', pinned: false, source_chat: null, created_at: 'x', updated_at: 'x' };
    const calls = mockHub((c) => {
      if (c.method === 'GET') return { body: { memories: [fact], count: 1 } };
      if (c.method === 'DELETE' && c.url.pathname.endsWith('/memory')) return { body: { deleted: 1 } };
      if (c.method === 'DELETE') return { status: 204 };
      return { body: { ...fact, ...(c.json as object) } };
    });
    expect(await memoryList(client, 'salaire')).toEqual([fact]);
    expect(calls[0].url.searchParams.get('q')).toBe('salaire');
    expect((await memoryCreate(client, 'X', true)).pinned).toBe(true);
    expect(calls[1].json).toEqual({ fact: 'X', pinned: true });
    expect((await memoryUpdate(client, 'm1', { pinned: true })).pinned).toBe(true);
    expect(calls[2]).toMatchObject({ method: 'PATCH' });
    expect(calls[2].url.pathname).toBe('/api/assistant/memory/m1');
    await memoryDelete(client, 'm1');
    expect(calls[3].method).toBe('DELETE');
    expect(await memoryForgetAll(client)).toEqual({ deleted: 1 });
    expect(calls[4].url.searchParams.get('confirm')).toBe('true');
  });
});
