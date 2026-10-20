import { afterEach, describe, expect, it, vi } from 'vitest';
import { DIRECT_UPLOAD_THRESHOLD, DIRECT_UPLOAD_UNSUPPORTED, HubClient, HubError, cronJobPath, normalizeCronJobs } from '../api';

// Drive presign / direct uploads, cron schedules and LLM audio + rerank, against the hub contract.

interface SentXhr {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

/** Minimal XMLHttpRequest stand-in (vitest runs in node, which has none). */
function stubXhr(respond: (req: SentXhr) => { status: number; text?: string }) {
  const sent: SentXhr[] = [];
  class FakeXhr {
    status = 0;
    statusText = '';
    responseText = '';
    upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    private req: SentXhr = { method: '', url: '', headers: {}, body: undefined };
    open(method: string, url: string) {
      this.req.method = method;
      this.req.url = url;
    }
    setRequestHeader(k: string, v: string) {
      this.req.headers[k] = v;
    }
    send(body: unknown) {
      this.req.body = body;
      sent.push(this.req);
      const r = respond(this.req);
      queueMicrotask(() => {
        this.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 2 });
        if (r.status === 0) {
          this.onerror?.();
          return;
        }
        this.status = r.status;
        this.responseText = r.text ?? '';
        this.onload?.();
      });
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXhr);
  return sent;
}

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function bigFile(name = 'video.mp4', type = 'video/mp4'): File {
  return new File([new Uint8Array(DIRECT_UPLOAD_THRESHOLD + 1)], name, { type });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const client = new HubClient({ hubUrl: 'https://edge.example/api', token: 'tok' });

describe('drive presign', () => {
  it('GETs /drive/presign/{provider}/{key} with encoded key segments', async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe('https://edge.example/api/drive/presign/backblaze_b2/photos/my%20cat.jpg');
      expect(init.method).toBe('GET');
      expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
      return json({ url: 'https://b2.example/signed?x=1', provider: 'backblaze_b2', expires_s: 900 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const res = await client.drivePresign('photos/my cat.jpg', 'backblaze_b2');
    expect(res.url).toBe('https://b2.example/signed?x=1');
  });

  it('surfaces a 4xx as HubError so the view can fall back to the hub path', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'presign not supported' }, 501)));
    await expect(client.drivePresign('a.txt', 'gcp_gcs')).rejects.toMatchObject({ status: 501 });
  });
});

describe('drive uploads', () => {
  it('keeps small files on POST /drive/upload (multipart, bearer)', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const sent = stubXhr(() => ({ status: 201, text: JSON.stringify({ key: 'a.txt', backend: 'cloudflare_r2', size: 3 }) }));
    const progress: number[] = [];
    const res = await client.driveUploadFile(new File(['abc'], 'a.txt'), { key: 'a.txt', onProgress: (p) => progress.push(p) });
    expect(res).toEqual({ key: 'a.txt', provider: 'cloudflare_r2', direct: false });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe('POST');
    expect(sent[0].url).toBe('https://edge.example/api/drive/upload');
    expect(sent[0].headers.Authorization).toBe('Bearer tok');
    expect(sent[0].body).toBeInstanceOf(FormData);
    expect(progress).toEqual([0.5]);
  });

  it('sends large files straight to the presigned URL with exactly the returned method and headers', async () => {
    const target = {
      provider: 'backblaze_b2',
      key: 'uploads/video.mp4',
      method: 'PUT',
      url: 'https://s3.b2.example/bucket/uploads/video.mp4?X-Amz-Signature=abc',
      headers: { 'Content-Type': 'video/mp4' },
      expires_at: '2026-10-20T12:00:00Z',
    };
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe('https://edge.example/api/drive/upload-url');
      expect(init.method).toBe('POST');
      expect(JSON.parse(init.body as string)).toEqual({
        filename: 'video.mp4',
        size: DIRECT_UPLOAD_THRESHOLD + 1,
        content_type: 'video/mp4',
        prefix: 'uploads',
      });
      return json(target);
    });
    vi.stubGlobal('fetch', fetchMock);
    const sent = stubXhr(() => ({ status: 200 }));
    const file = bigFile();
    const res = await client.driveUploadFile(file, { prefix: 'uploads' });
    expect(res).toEqual({ key: 'uploads/video.mp4', provider: 'backblaze_b2', direct: true });
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe('PUT');
    expect(sent[0].url).toBe(target.url);
    // No bearer token to the storage backend: only the signed headers.
    expect(sent[0].headers).toEqual({ 'Content-Type': 'video/mp4' });
    const body = sent[0].body as Blob;
    expect(body.size).toBe(file.size);
    expect(body.type).toBe('');
  });

  it.each([501, 422, 404])('fails clearly (and never proxies) when upload-url answers %i', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'nope' }, status)));
    const sent = stubXhr(() => ({ status: 201, text: '{}' }));
    const err = await client.driveUploadFile(bigFile()).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(HubError);
    expect((err as HubError).message).toBe(DIRECT_UPLOAD_UNSUPPORTED);
    expect(DIRECT_UPLOAD_UNSUPPORTED).toBe('Large uploads need a storage backend with direct uploads (B2 or R2)');
    expect(sent).toHaveLength(0);
  });

  it('keeps other upload-url failures as they are, still without proxying', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'all storage backends failed' }, 503)));
    const sent = stubXhr(() => ({ status: 201, text: '{}' }));
    await expect(client.driveUploadFile(bigFile())).rejects.toMatchObject({ status: 503, message: 'all storage backends failed' });
    expect(sent).toHaveLength(0);
  });

  it('reports a rejected direct PUT and a CORS/network failure', async () => {
    const target = { provider: 'cloudflare_r2', key: 'k', method: 'PUT', url: 'https://r2.example/k', headers: {}, expires_at: null };
    vi.stubGlobal('fetch', vi.fn(async () => json(target)));
    stubXhr(() => ({ status: 403, text: '<Error><Code>SignatureDoesNotMatch</Code></Error>' }));
    await expect(client.driveUploadFile(bigFile())).rejects.toThrow(/Direct upload to cloudflare_r2 failed: 403.*SignatureDoesNotMatch/);
    stubXhr(() => ({ status: 0 }));
    await expect(client.driveUploadFile(bigFile())).rejects.toThrow('Direct upload to cloudflare_r2 failed (network or CORS error)');
  });
});

describe('cron', () => {
  it('normalises GET /cron/jobs with per-provider errors', () => {
    const l = normalizeCronJobs({ jobs: [{ id: '1', name: 'n', provider: 'qstash' }], errors: { github_actions: 'token missing' } });
    expect(l.jobs).toHaveLength(1);
    expect(l.errors).toEqual({ github_actions: 'token missing' });
    expect(normalizeCronJobs([{ id: '2' }]).jobs).toHaveLength(1);
    expect(normalizeCronJobs(null)).toEqual({ jobs: [], errors: {} });
    expect(cronJobPath('qstash', 'scd/1')).toBe('/cron/jobs/qstash/scd%2F1');
  });

  it('uses the hub cron routes', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push(`${init.method} ${url}${init.body ? ' ' + String(init.body) : ''}`);
        if (url.endsWith('/cron/providers')) return json({ providers: [{ name: 'qstash', enabled: true, healthy: true, description: 'Upstash' }] });
        if (init.method === 'DELETE') return new Response(null, { status: 204 });
        if (url.endsWith('/trigger')) return json({ triggered: true }, 202);
        if (init.method === 'POST') return json({ id: 'j1', name: 'ping', provider: 'qstash', schedule: '0 * * * *' }, 201);
        return json({ jobs: [{ id: 'j1', name: 'ping', provider: 'qstash' }], errors: {} });
      }),
    );
    const providers = await client.cronProviders();
    expect(providers[0]).toMatchObject({ name: 'qstash', healthy: true });
    const listing = await client.cronJobs('qstash');
    expect(listing.jobs[0].id).toBe('j1');
    await client.cronJobs();
    const job = await client.createCronJob({ provider: 'qstash', name: 'ping', schedule: '0 * * * *', target: 'https://x.example', method: 'POST', body: '{"a":1}' });
    expect(job.id).toBe('j1');
    await expect(client.deleteCronJob('qstash', 'j1')).resolves.toBeUndefined();
    await client.triggerCronJob('qstash', 'j1');
    expect(calls).toEqual([
      'GET https://edge.example/api/cron/providers',
      'GET https://edge.example/api/cron/jobs?provider=qstash',
      'GET https://edge.example/api/cron/jobs',
      'POST https://edge.example/api/cron/jobs {"provider":"qstash","name":"ping","schedule":"0 * * * *","target":"https://x.example","method":"POST","body":"{\\"a\\":1}"}',
      'DELETE https://edge.example/api/cron/jobs/qstash/j1',
      'POST https://edge.example/api/cron/jobs/qstash/j1/trigger',
    ]);
  });
});

describe('llm audio and rerank', () => {
  it('POSTs multipart /llm/transcribe with file, model and language', async () => {
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe('https://edge.example/api/llm/transcribe');
      expect(init.method).toBe('POST');
      const headers = init.headers as Record<string, string>;
      expect(headers.Authorization).toBe('Bearer tok');
      // The browser sets the multipart boundary itself.
      expect(headers['Content-Type']).toBeUndefined();
      const form = init.body as FormData;
      expect(form).toBeInstanceOf(FormData);
      expect((form.get('file') as File).name).toBe('recording.webm');
      expect(form.get('model')).toBe('whisper-large-v3');
      expect(form.get('language')).toBe('fr');
      return json({ text: 'bonjour', model: 'whisper-large-v3', language: 'fr', duration: 1.5 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const res = await client.transcribe(new Blob(['x'], { type: 'audio/webm' }), { model: 'whisper-large-v3', language: 'fr' });
    expect(res).toEqual({ text: 'bonjour', model: 'whisper-large-v3', language: 'fr', duration: 1.5 });
  });

  it('keeps a picked file name and omits empty options', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        const form = init.body as FormData;
        expect((form.get('file') as File).name).toBe('memo.m4a');
        expect(form.has('model')).toBe(false);
        expect(form.has('language')).toBe(false);
        return json({ text: 'hi' });
      }),
    );
    expect((await client.transcribe(new File(['x'], 'memo.m4a', { type: 'audio/mp4' }))).text).toBe('hi');
  });

  it('POSTs /llm/rerank and returns the results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        expect(url).toBe('https://edge.example/api/llm/rerank');
        expect(JSON.parse(init.body as string)).toEqual({ query: 'cats', documents: ['dogs', 'cats'], top_n: 1 });
        return json({ results: [{ index: 1, score: 0.93, document: 'cats' }] });
      }),
    );
    expect(await client.rerank({ query: 'cats', documents: ['dogs', 'cats'], top_n: 1 })).toEqual([{ index: 1, score: 0.93, document: 'cats' }]);
  });
});
