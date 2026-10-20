import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  HubClient,
  HubError,
  driveObjectPath,
  groupModels,
  groupQuotas,
  listOf,
  normalizeDriveUsage,
  normalizeListing,
} from '../api';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('normalisers', () => {
  it('groups router.quotas() output and flat lists', () => {
    const q = { provider: 'groq', used: 3, limit: 1000, unit: 'requests', window: 'day', source: 'router' };
    expect(groupQuotas({ groq: [q] }).groq).toHaveLength(1);
    expect(groupQuotas([q]).groq[0].limit).toBe(1000);
    expect(groupQuotas({ quotas: { r2: { ...q, provider: 'r2', unit: 'bytes' } } }).r2[0].unit).toBe('bytes');
    expect(groupQuotas({ bad: 'x' })).toEqual({});
  });

  it('groups models from router.list_models() and OpenAI style lists', () => {
    expect(groupModels({ groq: [{ id: 'llama-3.3-70b', provider: 'groq' }] }).groq[0].id).toBe('llama-3.3-70b');
    expect(groupModels({ ollama: ['qwen2.5:7b'] }).ollama[0]).toEqual({ id: 'qwen2.5:7b', provider: 'ollama' });
    expect(groupModels({ data: [{ id: 'm', provider: 'gemini' }] }).gemini).toHaveLength(1);
  });

  it('normalises storage listings including per-backend errors', () => {
    const l = normalizeListing({ cloudflare_r2: [{ key: 'a.txt', size: 1, backend: '' }], backblaze_b2: 'error' });
    expect(l.objects[0].backend).toBe('cloudflare_r2');
    expect(l.errors).toEqual({ backblaze_b2: 'error' });
    expect(normalizeListing({ objects: [{ key: 'b', size: 2, backend: 'gcp_gcs' }], errors: {} }).objects).toHaveLength(1);
  });

  it('extracts arrays from wrapped bodies', () => {
    expect(listOf([1, 2], 'x')).toEqual([1, 2]);
    expect(listOf({ tasks: [1] }, 'tasks')).toEqual([1]);
    expect(listOf({ items: [3] }, 'tasks')).toEqual([3]);
    expect(listOf(null, 'tasks')).toEqual([]);
  });
});

describe('HubClient', () => {
  const client = new HubClient({ hubUrl: 'https://hub.example/', token: 'tok' });

  it('builds URLs and keeps the websocket token out of the URL', () => {
    expect(client.url('/drive/files', { prefix: '', q: 'cat', n: 2 })).toBe('https://hub.example/drive/files?q=cat&n=2');
    expect(decodeURIComponent(client.wsUrl(['router.*']))).toBe('wss://hub.example/ws?types=router.*');
    expect(client.wsProtocols()).toEqual(['bearer', 'tok']);
    const odd = new HubClient({ hubUrl: 'https://hub.example/', token: 'a b=' });
    expect(odd.wsProtocols()).toBeUndefined();
    expect(decodeURIComponent(odd.wsUrl([]))).toBe('wss://hub.example/ws?token=a+b=');
  });

  it('sends the bearer token and reads X-Routed-Via', async () => {
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
      expect(JSON.parse(init.body as string).stream).toBe(false);
      return new Response(
        JSON.stringify({ id: '1', model: 'm', content: 'hi', role: 'assistant', usage: {}, routed_via: 'groq', attempts: [] }),
        { status: 200, headers: { 'content-type': 'application/json', 'X-Routed-Via': 'freellmapi:groq' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    const { response, routedVia } = await client.chat({ messages: [{ role: 'user', content: 'hi' }] });
    expect(response.content).toBe('hi');
    expect(routedVia).toBe('freellmapi:groq');
  });

  it('maps FastAPI error bodies to HubError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'all providers failed' }), { status: 503, headers: { 'content-type': 'application/json' } })),
    );
    await expect(client.quotas()).rejects.toMatchObject({ status: 503, message: 'all providers failed' });
    await expect(client.quotas()).rejects.toBeInstanceOf(HubError);
  });

  it('streams chunks and stops at [DONE]', async () => {
    const sse =
      'data: {"delta":"he","model":"m","routed_via":"groq"}\n\ndata: {"delta":"y","model":"m","routed_via":"groq"}\n\ndata: [DONE]\n\n';
    vi.stubGlobal('fetch', vi.fn(async () => new Response(sse, { status: 200, headers: { 'content-type': 'text/event-stream', 'X-Routed-Via': 'groq' } })));
    let text = '';
    let rv = '';
    await client.stream({ messages: [] }, { onChunk: (c) => (text += c.delta), onRoutedVia: (r) => (rv = r) });
    expect(text).toBe('hey');
    expect(rv).toBe('groq');
  });

  it('raises on an SSE error event', async () => {
    const sse = 'event: error\ndata: {"detail":"No provider available"}\n\n';
    vi.stubGlobal('fetch', vi.fn(async () => new Response(sse, { status: 200, headers: { 'content-type': 'text/event-stream' } })));
    await expect(client.stream({ messages: [] }, { onChunk: () => undefined })).rejects.toThrow('No provider available');
  });
});

describe('hub_api response shapes', () => {
  const client = new HubClient({ hubUrl: 'https://hub.example', token: 'tok' });

  it('reads GET /llm/quotas and GET /llm/models', () => {
    const q = { provider: 'groq', used: 3, limit: 1000, unit: 'requests', window: 'day', source: 'router', remaining: 997 };
    const quotas = groupQuotas({ kind: 'llm', providers: { groq: { quotas: [q], breaker: 'closed', exhausted_until: null } } });
    expect(Object.keys(quotas)).toEqual(['groq']);
    expect(quotas.groq[0].limit).toBe(1000);
    const models = groupModels({ providers: { groq: [{ id: 'llama-3.3-70b', provider: 'groq' }] }, chain: ['groq'] });
    expect(Object.keys(models)).toEqual(['groq']);
  });

  it('reads GET /drive/files, /drive/usage and builds object paths', () => {
    const l = normalizeListing({ items: [{ key: 'a/b.txt', size: 1, backend: 'cloudflare_r2' }], count: 1, errors: { gcp_gcs: 'down' } });
    expect(l.objects).toHaveLength(1);
    expect(l.errors).toEqual({ gcp_gcs: 'down' });
    const usage = normalizeDriveUsage({
      backends: [{ provider: 'cloudflare_r2', used: 5, limit: 10e9, remaining: 10e9 - 5, fraction_used: 0, source: 'provider' }],
      total: { used: 5, limit: 10e9 },
      strategy: 'most_free',
    });
    expect(usage.cloudflare_r2).toMatchObject({ used: 5, limit: 10e9, unit: 'bytes', source: 'provider' });
    expect(driveObjectPath('cloudflare_r2', 'photos/my cat.jpg')).toBe('/drive/files/cloudflare_r2/photos/my%20cat.jpg');
  });

  it('stops at the hub `event: done` and ignores its payload', async () => {
    const sse = [
      'event: chunk',
      'data: {"delta":"he","model":"m","routed_via":"groq"}',
      '',
      'event: chunk',
      'data: {"delta":"y","model":"m","routed_via":"groq"}',
      '',
      'event: done',
      'data: {"routed_via":"groq","chunks":2}',
      '',
      '',
    ].join('\n');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(sse, { status: 200, headers: { 'content-type': 'text/event-stream' } })));
    let text = '';
    let n = 0;
    await client.stream({ messages: [] }, { onChunk: (c) => ((text += c.delta), n++) });
    expect(text).toBe('hey');
    expect(n).toBe(2);
  });

  it('uses the hub route names', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push(`${init.method} ${url}${init.body ? ' ' + String(init.body) : ''}`);
        return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
      }),
    );
    await client.health();
    await client.dnsQueryLog(50, true);
    await client.reorderPlugins('llm', ['groq', 'cerebras']);
    await client.driveDelete('a/b.txt', 'backblaze_b2');
    await client.driveList('', 'cat');
    expect(calls).toEqual([
      'GET https://hub.example/healthz',
      'GET https://hub.example/dns/queries?limit=50&blocked_only=true',
      'POST https://hub.example/plugins/reorder {"kind":"llm","names":["groq","cerebras"]}',
      'DELETE https://hub.example/drive/files/backblaze_b2/a/b.txt',
      'GET https://hub.example/drive/search?q=cat',
    ]);
  });
});
