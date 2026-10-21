// Typed client for hub_api. Every endpoint used here is listed in API_ASSUMPTIONS.md.
// Response normalisers accept a couple of plausible shapes so the PWA keeps working while
// hub_api and this app are reconciled.

import type { HubSettings } from './settings';
import { readSSE } from './sse';
import { normalizeMoneyOverview } from './money';
import type {
  AgentTask,
  AgentTaskCreate,
  BudgetItem,
  BudgetReport,
  Drop,
  DropCreate,
  Expense,
  ExpenseCreate,
  ExpenseListing,
  ExpensePatch,
  Note,
  NotebookCount,
  NoteCreate,
  NoteListing,
  NotePatch,
  RagAnswer,
  RagAskRequest,
  RagDoc,
  RagDocListing,
  ReceiptResult,
  ShortLink,
  ShortLinkCreate,
  Task,
  TaskCreate,
  TaskPatch,
  VoiceMode,
  VoiceNoteResult,
  AccountCreate,
  AccountPatch,
  CategoryBudget,
  ExpenseCategory,
  Income,
  IncomeCreate,
  IncomePatch,
  MoneyAccount,
  MoneyOverview,
  RecurringCreate,
  RecurringExpense,
  RecurringPatch,
  VoiceMoneyKind,
  VoiceMoneyResult,
  ChatChunk,
  Digest,
  DigestPeriod,
  InboxStatus,
  InboxSyncRequest,
  InboxSyncResult,
  ChatRequest,
  ChatResponse,
  CronJob,
  CronJobCreate,
  CronProvider,
  DBProvider,
  DirectUploadTarget,
  DNSQueryLogEntry,
  DNSStatus,
  HealthStatus,
  HubEvent,
  ModelInfo,
  PresignedUrl,
  ProviderStatus,
  QueryResult,
  QuotaInfo,
  RerankRequest,
  RerankResult,
  StoredObject,
  TableInfo,
  TranscriptionResult,
  UploadUrlRequest,
  VMInstance,
} from './types';

/**
 * The hub runs serverless with a ~4.5 MB request/response limit. Files above this size never go
 * through `POST /drive/upload`: they are sent straight to the storage backend via `POST /drive/upload-url`.
 */
export const DIRECT_UPLOAD_THRESHOLD = 4 * 1024 * 1024;

export const DIRECT_UPLOAD_UNSUPPORTED = 'Large uploads need a storage backend with direct uploads (B2 or R2)';

export class HubError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = 'HubError';
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  query?: Query;
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  accept?: string;
}

function detailMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : JSON.stringify(d)))
        .join('; ');
    }
    if (detail && typeof detail === 'object' && 'message' in detail) return String((detail as { message: unknown }).message);
  }
  if (typeof body === 'string' && body) return body.slice(0, 300);
  return fallback;
}

export interface ChatResult {
  response: ChatResponse;
  /** Value of the X-Routed-Via header (falls back to response.routed_via). */
  routedVia: string;
}

export interface StreamHandlers {
  onChunk: (chunk: ChatChunk) => void;
  onRoutedVia?: (routedVia: string) => void;
}

export interface DriveListing {
  objects: StoredObject[];
  errors: Record<string, string>;
}

export interface UploadOptions {
  key?: string;
  provider?: string;
  onProgress?: (fraction: number) => void;
}

export interface SmartUploadOptions extends UploadOptions {
  prefix?: string;
}

export interface SmartUploadResult {
  key: string;
  provider: string;
  /** True when the bytes went straight to the storage backend (presigned upload). */
  direct: boolean;
}

export interface CronJobListing {
  jobs: CronJob[];
  errors: Record<string, string>;
}

export interface TranscribeOptions {
  model?: string;
  language?: string;
  /** File name for the multipart part; recorded blobs have none. */
  filename?: string;
}

interface XhrResult {
  status: number;
  statusText: string;
  text: string;
}

/** XMLHttpRequest wrapped in a Promise, for upload progress events (fetch has none). */
function xhrSend(
  method: string,
  url: string,
  headers: Record<string, string>,
  body: XMLHttpRequestBodyInit,
  onProgress?: (fraction: number) => void,
): Promise<XhrResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url);
    for (const [k, v] of Object.entries(headers)) {
      try {
        xhr.setRequestHeader(k, v);
      } catch {
        // Headers the browser manages itself (Content-Length, Host) cannot be set; it sends them anyway.
      }
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new TypeError('network error'));
    xhr.onload = () => resolve({ status: xhr.status, statusText: xhr.statusText, text: xhr.responseText });
    xhr.send(body);
  });
}

function parseBody(text: string): unknown {
  try {
    return text ? JSON.parse(text) : undefined;
  } catch {
    return text;
  }
}

export class HubClient {
  readonly baseUrl: string;
  readonly token: string;

  constructor(settings: HubSettings) {
    this.baseUrl = settings.hubUrl.replace(/\/+$/, '');
    this.token = settings.token;
  }

  url(path: string, query?: Query): string {
    const u = new URL(this.baseUrl + path);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, String(v));
      }
    }
    return u.toString();
  }

  /** True when the token can travel as a WebSocket subprotocol (RFC 7230 token characters). */
  private tokenFitsSubprotocol(): boolean {
    return !!this.token && /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/.test(this.token);
  }

  /**
   * Subprotocols for the /ws upgrade: ["bearer", token] keeps the token out of the URL,
   * which Cloud Run request logs and proxies record in full.
   */
  wsProtocols(): string[] | undefined {
    return this.tokenFitsSubprotocol() ? ['bearer', this.token as string] : undefined;
  }

  wsUrl(types: string[]): string {
    const u = new URL(this.baseUrl + '/ws');
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    if (this.token && !this.tokenFitsSubprotocol()) u.searchParams.set('token', this.token);
    if (types.length) u.searchParams.set('types', types.join(','));
    return u.toString();
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { ...extra };
    if (this.token) h.Authorization = `Bearer ${this.token}`;
    return h;
  }

  async raw(method: string, path: string, opts: RequestOptions = {}): Promise<Response> {
    const headers = this.headers({ Accept: opts.accept ?? 'application/json' });
    let body: BodyInit | undefined;
    if (opts.form) body = opts.form;
    else if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.json);
    }
    let res: Response;
    try {
      res = await fetch(this.url(path, opts.query), { method, headers, body, signal: opts.signal });
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') throw err;
      throw new HubError(`Cannot reach hub at ${this.baseUrl} (${err instanceof Error ? err.message : String(err)})`, 0);
    }
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      let parsed: unknown = text;
      try {
        parsed = text ? JSON.parse(text) : undefined;
      } catch {
        parsed = text;
      }
      throw new HubError(detailMessage(parsed, `${res.status} ${res.statusText}`), res.status, parsed);
    }
    return res;
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const res = await this.raw(method, path, opts);
    if (res.status === 204) return undefined as T;
    const type = res.headers.get('content-type') ?? '';
    if (type.includes('application/json')) return (await res.json()) as T;
    return (await res.text()) as unknown as T;
  }

  // ------------------------------------------------------------------ system

  health(signal?: AbortSignal): Promise<unknown> {
    return this.request('GET', '/healthz', { signal });
  }

  /** `GET /events/recent`: the hub's event history, oldest first (polling fallback for /ws). */
  async recentEvents(limit: number, types: string[], signal?: AbortSignal): Promise<HubEvent[]> {
    const body = await this.request<unknown>('GET', '/events/recent', { query: { limit, types: types.join(',') }, signal });
    return listOf<HubEvent>(body, 'events');
  }

  // ------------------------------------------------------------------ LLM

  async chat(req: ChatRequest, signal?: AbortSignal): Promise<ChatResult> {
    const res = await this.raw('POST', '/llm/chat', { json: { ...req, stream: false }, signal });
    const response = (await res.json()) as ChatResponse;
    const routedVia = res.headers.get('X-Routed-Via') ?? response.routed_via ?? '';
    return { response, routedVia };
  }

  async stream(req: ChatRequest, handlers: StreamHandlers, signal?: AbortSignal): Promise<void> {
    const res = await this.raw('POST', '/llm/stream', { json: { ...req, stream: true }, signal, accept: 'text/event-stream' });
    const header = res.headers.get('X-Routed-Via');
    if (header) handlers.onRoutedVia?.(header);
    // Serverless deployments may buffer the reply and answer with one plain JSON ChatResponse.
    if ((res.headers.get('content-type') ?? '').includes('application/json')) {
      const response = (await res.json()) as ChatResponse;
      handlers.onChunk({
        delta: typeof response.content === 'string' ? response.content : '',
        model: response.model ?? '',
        finish_reason: response.finish_reason ?? null,
        usage: response.usage ?? null,
        routed_via: header || response.routed_via || '',
      });
      return;
    }
    if (!res.body) throw new HubError('Streaming is not supported by this browser', 0);
    // hub_api sends `event: chunk` (ChatChunk JSON) ..., then `event: done` or `event: error`.
    // A bare `data: [DONE]` (OpenAI style) also ends the stream.
    for await (const msg of readSSE(res.body, signal)) {
      if (msg.data === '[DONE]' || msg.event === 'done') return;
      if (msg.event === 'error') {
        let parsed: unknown = msg.data;
        try {
          parsed = JSON.parse(msg.data);
        } catch {
          parsed = msg.data;
        }
        throw new HubError(detailMessage(parsed, 'Stream failed'), 502, parsed);
      }
      let chunk: ChatChunk;
      try {
        chunk = JSON.parse(msg.data) as ChatChunk;
      } catch {
        continue;
      }
      handlers.onChunk(chunk);
    }
  }

  async quotas(signal?: AbortSignal): Promise<Record<string, QuotaInfo[]>> {
    return groupQuotas(await this.request<unknown>('GET', '/llm/quotas', { signal }));
  }

  async models(signal?: AbortSignal): Promise<Record<string, ModelInfo[]>> {
    return groupModels(await this.request<unknown>('GET', '/llm/models', { signal }));
  }

  /** `POST /llm/transcribe` (multipart): speech to text. */
  transcribe(audio: Blob, opts: TranscribeOptions = {}, signal?: AbortSignal): Promise<TranscriptionResult> {
    const form = new FormData();
    const name = opts.filename ?? (typeof File !== 'undefined' && audio instanceof File ? audio.name : 'recording.webm');
    form.append('file', audio, name);
    if (opts.model) form.append('model', opts.model);
    if (opts.language) form.append('language', opts.language);
    return this.request<TranscriptionResult>('POST', '/llm/transcribe', { form, signal });
  }

  /** `POST /llm/rerank`: documents ordered by relevance to `query`. */
  async rerank(req: RerankRequest, signal?: AbortSignal): Promise<RerankResult[]> {
    const body = await this.request<unknown>('POST', '/llm/rerank', { json: req, signal });
    return listOf<RerankResult>(body, 'results');
  }

  // ------------------------------------------------------------------ agent

  async agentTasks(signal?: AbortSignal): Promise<AgentTask[]> {
    const body = await this.request<unknown>('GET', '/agent/tasks', { signal });
    return listOf<AgentTask>(body, 'tasks');
  }

  createAgentTask(task: AgentTaskCreate): Promise<AgentTask> {
    return this.request<AgentTask>('POST', '/agent/tasks', { json: task });
  }

  cancelAgentTask(id: string): Promise<AgentTask> {
    return this.request<AgentTask>('POST', `/agent/tasks/${encodeURIComponent(id)}/cancel`);
  }

  // ------------------------------------------------------------------ drive

  async driveList(prefix: string, q: string, signal?: AbortSignal): Promise<DriveListing> {
    const needle = q.trim();
    if (needle) {
      return normalizeListing(await this.request<unknown>('GET', '/drive/search', { query: { q: needle, prefix }, signal }));
    }
    return normalizeListing(await this.request<unknown>('GET', '/drive/files', { query: { prefix }, signal }));
  }

  async driveCapacity(signal?: AbortSignal): Promise<Record<string, QuotaInfo>> {
    return normalizeDriveUsage(await this.request<unknown>('GET', '/drive/usage', { signal }));
  }

  /** Multipart upload through the hub (XHR so the UI can show progress). Only for files up to DIRECT_UPLOAD_THRESHOLD. */
  async driveUpload(file: File, opts: UploadOptions = {}): Promise<StoredObject> {
    const form = new FormData();
    form.append('file', file, file.name);
    if (opts.key) form.append('key', opts.key);
    if (opts.provider) form.append('provider', opts.provider);
    let res: XhrResult;
    try {
      res = await xhrSend('POST', this.url('/drive/upload'), this.headers({ Accept: 'application/json' }), form, opts.onProgress);
    } catch {
      throw new HubError(`Cannot reach hub at ${this.baseUrl}`, 0);
    }
    const parsed = parseBody(res.text);
    if (res.status >= 200 && res.status < 300) return parsed as StoredObject;
    throw new HubError(detailMessage(parsed, `${res.status} ${res.statusText}`), res.status, parsed);
  }

  /** `POST /drive/upload-url`: a presigned target for sending a file straight to a storage backend. */
  driveUploadUrl(req: UploadUrlRequest, signal?: AbortSignal): Promise<DirectUploadTarget> {
    return this.request<DirectUploadTarget>('POST', '/drive/upload-url', { json: req, signal });
  }

  /**
   * Sends `file` to a presigned target with exactly the returned method and headers (no bearer
   * token: the URL carries its own signature).
   */
  async driveDirectUpload(file: Blob, target: DirectUploadTarget, onProgress?: (fraction: number) => void): Promise<void> {
    // A typeless view of the bytes, so the browser adds no Content-Type beyond the returned headers.
    const body = file.slice(0, file.size, '');
    let res: XhrResult;
    try {
      res = await xhrSend(target.method || 'PUT', target.url, target.headers ?? {}, body, onProgress);
    } catch {
      throw new HubError(`Direct upload to ${target.provider} failed (network or CORS error)`, 0);
    }
    if (res.status < 200 || res.status >= 300) {
      const parsed = parseBody(res.text);
      const detail = typeof parsed === 'string' ? parsed.slice(0, 200) : detailMessage(parsed, '');
      throw new HubError(
        `Direct upload to ${target.provider} failed: ${res.status} ${res.statusText}`.trim() + (detail ? ` (${detail})` : ''),
        res.status,
        parsed,
      );
    }
  }

  /**
   * Uploads through the hub up to DIRECT_UPLOAD_THRESHOLD, and straight to the storage backend
   * above it. A large file is never sent through the hub, which would reject it anyway.
   */
  async driveUploadFile(file: File, opts: SmartUploadOptions = {}): Promise<SmartUploadResult> {
    if (file.size <= DIRECT_UPLOAD_THRESHOLD) {
      const obj = await this.driveUpload(file, opts);
      return { key: obj?.key ?? opts.key ?? file.name, provider: obj?.backend ?? '', direct: false };
    }
    let target: DirectUploadTarget;
    try {
      target = await this.driveUploadUrl({
        filename: file.name,
        size: file.size,
        content_type: file.type || 'application/octet-stream',
        ...(opts.prefix ? { prefix: opts.prefix } : {}),
        ...(opts.key ? { key: opts.key } : {}),
        ...(opts.provider ? { provider: opts.provider } : {}),
      });
    } catch (err) {
      if (err instanceof HubError && [404, 405, 422, 501].includes(err.status)) {
        throw new HubError(DIRECT_UPLOAD_UNSUPPORTED, err.status, err.body);
      }
      throw err;
    }
    await this.driveDirectUpload(file, target, opts.onProgress);
    return { key: target.key, provider: target.provider, direct: true };
  }

  /** `GET /drive/presign/{provider}/{key}`: a short-lived URL that serves the object without the hub. */
  drivePresign(key: string, provider: string, signal?: AbortSignal, expiresS?: number): Promise<PresignedUrl> {
    return this.request<PresignedUrl>('GET', driveObjectPath(provider, key, '/drive/presign'), { signal, query: { expires_s: expiresS } });
  }

  /** `GET /drive/locate/{key}`: which backend holds `key` (used for voice-note audio, stored without a provider). */
  driveLocate(key: string, signal?: AbortSignal): Promise<StoredObject> {
    const segments = key.split('/').filter(Boolean).map(encodeURIComponent);
    return this.request<StoredObject>('GET', `/drive/locate/${segments.join('/')}`, { signal });
  }

  async driveDownload(key: string, provider: string): Promise<Blob> {
    const res = await this.raw('GET', driveObjectPath(provider, key), { accept: '*/*' });
    return res.blob();
  }

  driveDelete(key: string, provider: string): Promise<unknown> {
    return this.request('DELETE', driveObjectPath(provider, key));
  }

  // ------------------------------------------------------------------ cron / schedules

  async cronProviders(signal?: AbortSignal): Promise<CronProvider[]> {
    const body = await this.request<unknown>('GET', '/cron/providers', { signal });
    return listOf<CronProvider>(body, 'providers');
  }

  async cronJobs(provider?: string, signal?: AbortSignal): Promise<CronJobListing> {
    return normalizeCronJobs(await this.request<unknown>('GET', '/cron/jobs', { query: { provider }, signal }));
  }

  createCronJob(job: CronJobCreate): Promise<CronJob> {
    return this.request<CronJob>('POST', '/cron/jobs', { json: job });
  }

  deleteCronJob(provider: string, id: string): Promise<void> {
    return this.request<void>('DELETE', cronJobPath(provider, id));
  }

  triggerCronJob(provider: string, id: string): Promise<unknown> {
    return this.request('POST', `${cronJobPath(provider, id)}/trigger`);
  }

  // ------------------------------------------------------------------ DNS & network

  dnsStatus(signal?: AbortSignal): Promise<DNSStatus> {
    return this.request<DNSStatus>('GET', '/dns/status', { signal });
  }

  dnsSetBlocking(enabled: boolean, durationS?: number): Promise<DNSStatus> {
    return this.request<DNSStatus>('POST', '/dns/blocking', { json: { enabled, duration_s: durationS ?? null } });
  }

  async dnsQueryLog(limit: number, blockedOnly: boolean, signal?: AbortSignal): Promise<DNSQueryLogEntry[]> {
    const body = await this.request<unknown>('GET', '/dns/queries', { query: { limit, blocked_only: blockedOnly }, signal });
    return listOf<DNSQueryLogEntry>(body, 'entries');
  }

  async tailscaleNodes(signal?: AbortSignal): Promise<VMInstance[]> {
    const body = await this.request<unknown>('GET', '/dns/nodes', { signal });
    return listOf<VMInstance>(body, 'nodes');
  }

  // ------------------------------------------------------------------ databases

  async dbProviders(signal?: AbortSignal): Promise<DBProvider[]> {
    const body = await this.request<unknown>('GET', '/db/providers', { signal });
    return listOf<DBProvider>(body, 'providers');
  }

  async dbTables(provider: string, signal?: AbortSignal): Promise<TableInfo[]> {
    const body = await this.request<unknown>('GET', `/db/${encodeURIComponent(provider)}/tables`, { signal });
    return listOf<TableInfo>(body, 'tables');
  }

  dbDescribe(provider: string, table: string, signal?: AbortSignal): Promise<TableInfo> {
    return this.request<TableInfo>('GET', `/db/${encodeURIComponent(provider)}/tables/${encodeURIComponent(table)}`, { signal });
  }

  dbQuery(provider: string, statement: string, opts: { write: boolean; limit: number }, signal?: AbortSignal): Promise<QueryResult> {
    return this.request<QueryResult>('POST', `/db/${encodeURIComponent(provider)}/query`, {
      json: { statement, params: null, limit: opts.limit, write: opts.write },
      signal,
    });
  }

  dbUsage(provider: string, signal?: AbortSignal): Promise<QuotaInfo> {
    return this.request<QuotaInfo>('GET', `/db/${encodeURIComponent(provider)}/usage`, { signal });
  }

  // ------------------------------------------------------------------ plugins

  async plugins(kind?: string, signal?: AbortSignal): Promise<ProviderStatus[]> {
    const body = await this.request<unknown>('GET', '/plugins', { query: { kind }, signal });
    return listOf<ProviderStatus>(body, 'plugins');
  }

  setPluginEnabled(name: string, enabled: boolean): Promise<unknown> {
    return this.request('POST', `/plugins/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`);
  }

  reorderPlugins(kind: string, order: string[]): Promise<{ chain: string[] }> {
    return this.request<{ chain: string[] }>('POST', '/plugins/reorder', { json: { kind, names: order } });
  }

  checkPluginHealth(name: string): Promise<HealthStatus> {
    return this.request<HealthStatus>('POST', `/plugins/${encodeURIComponent(name)}/health`);
  }

  resetPlugin(name: string): Promise<unknown> {
    return this.request('POST', `/plugins/${encodeURIComponent(name)}/reset`);
  }

  // ------------------------------------------------------------------ notes & tasks

  async notes(params: { notebook?: string; q?: string; limit?: number } = {}, signal?: AbortSignal): Promise<NoteListing> {
    const body = await this.request<unknown>('GET', '/notes', { query: { notebook: params.notebook, q: params.q?.trim(), limit: params.limit }, signal });
    return {
      notes: listOf<Note>(body, 'notes'),
      notebooks: isRecord(body) && Array.isArray(body.notebooks) ? (body.notebooks as NotebookCount[]) : [],
    };
  }

  note(id: string, signal?: AbortSignal): Promise<Note> {
    return this.request<Note>('GET', `/notes/${encodeURIComponent(id)}`, { signal });
  }

  createNote(note: NoteCreate): Promise<Note> {
    return this.request<Note>('POST', '/notes', { json: note });
  }

  updateNote(id: string, patch: NotePatch, signal?: AbortSignal): Promise<Note> {
    return this.request<Note>('PATCH', `/notes/${encodeURIComponent(id)}`, { json: patch, signal });
  }

  deleteNote(id: string): Promise<void> {
    return this.request<void>('DELETE', `/notes/${encodeURIComponent(id)}`);
  }

  /** `POST /notes/voice` (multipart): audio in, transcript + note and/or tasks out. */
  voiceNote(
    audio: Blob,
    opts: { mode?: VoiceMode; notebook?: string; language?: string; filename?: string } = {},
    signal?: AbortSignal,
  ): Promise<VoiceNoteResult> {
    const form = new FormData();
    const name = opts.filename ?? (typeof File !== 'undefined' && audio instanceof File ? audio.name : 'voice.webm');
    form.append('file', audio, name);
    form.append('mode', opts.mode ?? 'auto');
    if (opts.notebook) form.append('notebook', opts.notebook);
    if (opts.language) form.append('language', opts.language);
    return this.request<VoiceNoteResult>('POST', '/notes/voice', { form, signal });
  }

  async tasks(params: { status?: 'open' | 'done' | 'all'; due_before?: string } = {}, signal?: AbortSignal): Promise<Task[]> {
    const body = await this.request<unknown>('GET', '/tasks', { query: { status: params.status, due_before: params.due_before }, signal });
    return listOf<Task>(body, 'tasks');
  }

  createTask(task: TaskCreate): Promise<Task> {
    return this.request<Task>('POST', '/tasks', { json: task });
  }

  updateTask(id: string, patch: TaskPatch): Promise<Task> {
    return this.request<Task>('PATCH', `/tasks/${encodeURIComponent(id)}`, { json: patch });
  }

  deleteTask(id: string): Promise<void> {
    return this.request<void>('DELETE', `/tasks/${encodeURIComponent(id)}`);
  }

  // ------------------------------------------------------------------ expenses

  /** `POST /expenses/receipt` (multipart): a receipt photo read by a vision model. */
  scanReceipt(image: Blob, opts: { note?: string; filename?: string } = {}, signal?: AbortSignal): Promise<ReceiptResult> {
    const form = new FormData();
    const name = opts.filename ?? (typeof File !== 'undefined' && image instanceof File ? image.name : 'receipt.jpg');
    form.append('file', image, name);
    if (opts.note) form.append('note', opts.note);
    return this.request<ReceiptResult>('POST', '/expenses/receipt', { form, signal });
  }

  async expenses(month?: string, signal?: AbortSignal): Promise<ExpenseListing> {
    const body = await this.request<unknown>('GET', '/expenses', { query: { month }, signal });
    const s = isRecord(body) && isRecord(body.summary) ? body.summary : {};
    return {
      expenses: listOf<Expense>(body, 'expenses'),
      summary: {
        total_by_currency: (isRecord(s.total_by_currency) ? s.total_by_currency : {}) as Record<string, number>,
        by_category: (isRecord(s.by_category) ? s.by_category : {}) as Record<string, Record<string, number>>,
        count: typeof s.count === 'number' ? s.count : 0,
      },
    };
  }

  expense(id: string, signal?: AbortSignal): Promise<Expense> {
    return this.request<Expense>('GET', `/expenses/${encodeURIComponent(id)}`, { signal });
  }

  createExpense(expense: ExpenseCreate): Promise<Expense> {
    return this.request<Expense>('POST', '/expenses', { json: expense });
  }

  updateExpense(id: string, patch: ExpensePatch): Promise<Expense> {
    return this.request<Expense>('PATCH', `/expenses/${encodeURIComponent(id)}`, { json: patch });
  }

  deleteExpense(id: string): Promise<void> {
    return this.request<void>('DELETE', `/expenses/${encodeURIComponent(id)}`);
  }

  // ------------------------------------------------------------------ money (incomes, recurring, accounts, budgets)

  /** `GET /money/overview?month=`: income, spent, left, budgets, 6-month cash flow, accounts (in MAD). */
  async moneyOverview(month: string, signal?: AbortSignal): Promise<MoneyOverview> {
    return normalizeMoneyOverview(await this.request<unknown>('GET', '/money/overview', { query: { month }, signal }), month);
  }

  /** `POST /money/voice` (multipart): "j'ai paye 45 dh de taxi" -> expenses and incomes. */
  voiceMoney(audio: Blob, opts: { kind?: VoiceMoneyKind; language?: string; filename?: string } = {}, signal?: AbortSignal): Promise<VoiceMoneyResult> {
    const form = new FormData();
    const name = opts.filename ?? (typeof File !== 'undefined' && audio instanceof File ? audio.name : 'voice.webm');
    form.append('file', audio, name);
    form.append('kind', opts.kind ?? 'auto');
    if (opts.language) form.append('language', opts.language);
    return this.request<VoiceMoneyResult>('POST', '/money/voice', { form, signal });
  }

  createIncome(income: IncomeCreate): Promise<Income> {
    return this.request<Income>('POST', '/money/incomes', { json: income });
  }

  updateIncome(id: string, patch: IncomePatch): Promise<Income> {
    return this.request<Income>('PATCH', `/money/incomes/${encodeURIComponent(id)}`, { json: patch });
  }

  deleteIncome(id: string): Promise<void> {
    return this.request<void>('DELETE', `/money/incomes/${encodeURIComponent(id)}`);
  }

  createRecurring(item: RecurringCreate): Promise<RecurringExpense> {
    return this.request<RecurringExpense>('POST', '/money/recurring', { json: item });
  }

  updateRecurring(id: string, patch: RecurringPatch): Promise<RecurringExpense> {
    return this.request<RecurringExpense>('PATCH', `/money/recurring/${encodeURIComponent(id)}`, { json: patch });
  }

  deleteRecurring(id: string): Promise<void> {
    return this.request<void>('DELETE', `/money/recurring/${encodeURIComponent(id)}`);
  }

  createAccount(account: AccountCreate): Promise<MoneyAccount> {
    return this.request<MoneyAccount>('POST', '/money/accounts', { json: account });
  }

  updateAccount(id: string, patch: AccountPatch): Promise<MoneyAccount> {
    return this.request<MoneyAccount>('PATCH', `/money/accounts/${encodeURIComponent(id)}`, { json: patch });
  }

  deleteAccount(id: string): Promise<void> {
    return this.request<void>('DELETE', `/money/accounts/${encodeURIComponent(id)}`);
  }

  setBudget(category: ExpenseCategory, amount: number, currency = 'MAD'): Promise<CategoryBudget> {
    return this.request<CategoryBudget>('PUT', `/money/budgets/${encodeURIComponent(category)}`, { json: { amount, currency } });
  }

  deleteBudget(category: ExpenseCategory): Promise<void> {
    return this.request<void>('DELETE', `/money/budgets/${encodeURIComponent(category)}`);
  }

  // ------------------------------------------------------------------ Gmail receipts import

  /** `GET /inbox/receipts/status`: the configured mailboxes, their last sync or error, and the last run. */
  async inboxStatus(signal?: AbortSignal): Promise<InboxStatus> {
    const body = await this.request<unknown>('GET', '/inbox/receipts/status', { signal });
    const b = isRecord(body) ? body : {};
    const accounts = Array.isArray(b.accounts) ? b.accounts.filter(isRecord) : [];
    return {
      configured: b.configured === true,
      accounts: accounts.map((a) => ({
        name: String(a.name ?? ''),
        user: String(a.user ?? ''),
        host: String(a.host ?? ''),
        port: typeof a.port === 'number' ? a.port : 993,
        configured: a.configured === true,
        last_sync_at: typeof a.last_sync_at === 'string' ? a.last_sync_at : null,
        last_error: typeof a.last_error === 'string' ? a.last_error : null,
      })),
      problems: Array.isArray(b.problems) ? b.problems.filter((x): x is string => typeof x === 'string') : [],
      last_sync_at: typeof b.last_sync_at === 'string' ? b.last_sync_at : null,
      last_result: isRecord(b.last_result) ? (b.last_result as InboxStatus['last_result']) : null,
    };
  }

  /** `POST /inbox/receipts/sync`: reads receipt emails of every mailbox (or `opts.account`) into expenses (one call stays under ~50 s). */
  async syncGmailReceipts(opts: InboxSyncRequest = {}, signal?: AbortSignal): Promise<InboxSyncResult> {
    const body = await this.request<unknown>('POST', '/inbox/receipts/sync', { json: opts, signal });
    return normalizeSyncResult(body);
  }

  // ------------------------------------------------------------------ summary (digest)

  /** `GET /digest?period=week|month[&refresh=1]`: stats plus an LLM-written summary (cached ~1 h). */
  async digest(period: DigestPeriod = 'week', refresh = false, signal?: AbortSignal): Promise<Digest> {
    const body = await this.request<unknown>('GET', '/digest', { query: { period, refresh: refresh ? 1 : undefined }, signal });
    return normalizeDigest(body, period);
  }

  // ------------------------------------------------------------------ short links & drops

  async links(signal?: AbortSignal): Promise<ShortLink[]> {
    return listOf<ShortLink>(await this.request<unknown>('GET', '/links', { signal }), 'links');
  }

  createLink(link: ShortLinkCreate): Promise<ShortLink> {
    return this.request<ShortLink>('POST', '/links', { json: link });
  }

  deleteLink(code: string): Promise<void> {
    return this.request<void>('DELETE', `/links/${encodeURIComponent(code)}`);
  }

  async drops(signal?: AbortSignal): Promise<Drop[]> {
    return listOf<Drop>(await this.request<unknown>('GET', '/drops', { signal }), 'drops');
  }

  createDrop(drop: DropCreate): Promise<Drop> {
    return this.request<Drop>('POST', '/drops', { json: drop });
  }

  deleteDrop(code: string): Promise<void> {
    return this.request<void>('DELETE', `/drops/${encodeURIComponent(code)}`);
  }

  // ------------------------------------------------------------------ ask my drive (RAG)

  async ragDocs(signal?: AbortSignal): Promise<RagDocListing> {
    const body = await this.request<unknown>('GET', '/rag/docs', { signal });
    return {
      docs: listOf<RagDoc>(body, 'docs'),
      embedding_model: isRecord(body) && typeof body.embedding_model === 'string' ? body.embedding_model : '',
    };
  }

  /** `POST /rag/index`: one file per call (the hub has ~60 s per request). */
  async ragIndex(provider: string, key: string, signal?: AbortSignal): Promise<RagDoc> {
    const body = await this.request<{ doc: RagDoc }>('POST', '/rag/index', { json: { provider, key }, signal });
    return body.doc;
  }

  ragDelete(provider: string, key: string): Promise<void> {
    return this.request<void>('DELETE', driveObjectPath(provider, key, '/rag/docs'));
  }

  ragAsk(req: RagAskRequest, signal?: AbortSignal): Promise<RagAnswer> {
    return this.request<RagAnswer>('POST', '/rag/ask', { json: req, signal });
  }

  // ------------------------------------------------------------------ free-tier budget

  async budget(signal?: AbortSignal): Promise<BudgetReport> {
    const body = await this.request<unknown>('GET', '/budget', { signal });
    const s = isRecord(body) && isRecord(body.summary) ? body.summary : {};
    const n = (k: string) => (typeof s[k] === 'number' ? (s[k] as number) : 0);
    return {
      generated_at: isRecord(body) && typeof body.generated_at === 'string' ? body.generated_at : '',
      items: listOf<BudgetItem>(body, 'items'),
      summary: { ok: n('ok'), warn: n('warn'), over: n('over'), unknown: n('unknown') },
    };
  }
}

// ------------------------------------------------------------------ normalisers

/** `/drive/files/{provider}/{key}` (or another base): each key segment is encoded, `/` separators are kept. */
export function driveObjectPath(provider: string, key: string, base = '/drive/files'): string {
  const segments = key.split('/').filter(Boolean).map(encodeURIComponent);
  return `${base}/${encodeURIComponent(provider)}/${segments.join('/')}`;
}

export function cronJobPath(provider: string, id: string): string {
  return `/cron/jobs/${encodeURIComponent(provider)}/${encodeURIComponent(id)}`;
}

/** `POST /inbox/receipts/sync` reply with every list present. */
export function normalizeSyncResult(body: unknown): InboxSyncResult {
  const b = isRecord(body) ? body : {};
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  return {
    scanned: num(b.scanned),
    matched: num(b.matched),
    created: Array.isArray(b.created) ? (b.created as InboxSyncResult['created']) : [],
    skipped: Array.isArray(b.skipped) ? (b.skipped as InboxSyncResult['skipped']) : [],
    errors: Array.isArray(b.errors) ? (b.errors as InboxSyncResult['errors']) : [],
    remaining: num(b.remaining),
    dry_run: b.dry_run === true,
    accounts: Array.isArray(b.accounts) ? (b.accounts as InboxSyncResult['accounts']) : [],
  };
}

/** `GET /digest` reply with safe defaults for any missing stat. */
export function normalizeDigest(body: unknown, period: DigestPeriod = 'week'): Digest {
  const b = isRecord(body) ? body : {};
  const st = isRecord(b.stats) ? b.stats : {};
  const rec = (v: unknown): Record<string, unknown> => (isRecord(v) ? v : {});
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const numOrNull = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const strings = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);
  const e = rec(st.expenses);
  const t = rec(st.tasks);
  const n = rec(st.notes);
  const d = rec(st.drive);
  const l = rec(st.llm);
  return {
    period: b.period === 'month' || b.period === 'week' ? b.period : period,
    from: typeof b.from === 'string' ? b.from : '',
    to: typeof b.to === 'string' ? b.to : '',
    generated_at: typeof b.generated_at === 'string' ? b.generated_at : '',
    markdown: typeof b.markdown === 'string' ? b.markdown : '',
    stats: {
      expenses: {
        total_by_currency: rec(e.total_by_currency) as Record<string, number>,
        by_category: rec(e.by_category) as Record<string, Record<string, number>>,
        count: num(e.count),
        top_merchants: Array.isArray(e.top_merchants) ? (e.top_merchants as Digest['stats']['expenses']['top_merchants']) : [],
      },
      tasks: { done: num(t.done), open: num(t.open), overdue: num(t.overdue) },
      notes: { created: num(n.created), voice: num(n.voice) },
      drive: { uploaded_files: numOrNull(d.uploaded_files), uploaded_bytes: numOrNull(d.uploaded_bytes) },
      llm: { requests_by_provider: rec(l.requests_by_provider) as Record<string, number> },
    },
    highlights: strings(b.highlights),
    suggestions: strings(b.suggestions),
    source: typeof b.source === 'string' ? b.source : '',
    cached: b.cached === true,
  };
}

/** `GET /cron/jobs`: `{jobs: CronJob[], errors: {provider: message}}` (a flat array is accepted too). */
export function normalizeCronJobs(body: unknown): CronJobListing {
  const listing: CronJobListing = { jobs: listOf<CronJob>(body, 'jobs'), errors: {} };
  if (isRecord(body) && isRecord(body.errors)) for (const [k, v] of Object.entries(body.errors)) listing.errors[k] = String(v);
  return listing;
}

/**
 * hub_api `GET /drive/usage`: `{backends: [{provider, used, limit, source, ...}], total, strategy}`.
 * Also accepts `{provider: QuotaInfo | QuotaInfo[]}` (the bytes entry is used).
 */
export function normalizeDriveUsage(body: unknown): Record<string, QuotaInfo> {
  const out: Record<string, QuotaInfo> = {};
  if (isRecord(body) && Array.isArray(body.backends)) {
    for (const b of body.backends) {
      if (!isRecord(b) || typeof b.provider !== 'string' || typeof b.used !== 'number') continue;
      out[b.provider] = {
        provider: b.provider,
        used: b.used,
        limit: typeof b.limit === 'number' ? b.limit : null,
        unit: 'bytes',
        source: (b.source as QuotaInfo['source']) ?? 'router',
      };
    }
    return out;
  }
  for (const [name, list] of Object.entries(groupQuotas(body))) {
    const bytes = list.find((q) => q.unit === 'bytes') ?? list[0];
    if (bytes) out[name] = bytes;
  }
  return out;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

export function listOf<T>(body: unknown, key: string): T[] {
  if (Array.isArray(body)) return body as T[];
  if (isRecord(body)) {
    for (const k of [key, 'items', 'data', 'results']) {
      if (Array.isArray(body[k])) return body[k] as T[];
    }
  }
  return [];
}

/**
 * Accepts hub_api `GET /llm/quotas` (`{kind, providers: {name: {quotas: QuotaInfo[], breaker, exhausted_until}}}`),
 * `{provider: QuotaInfo[] | QuotaInfo}`, `{quotas: {...}}` or a flat `QuotaInfo[]`.
 */
export function groupQuotas(body: unknown): Record<string, QuotaInfo[]> {
  const out: Record<string, QuotaInfo[]> = {};
  const push = (name: string, q: unknown) => {
    if (isRecord(q) && typeof q.used === 'number') (out[name] ??= []).push({ ...(q as unknown as QuotaInfo), provider: String(q.provider ?? name) });
  };
  if (Array.isArray(body)) {
    for (const q of body) if (isRecord(q)) push(String(q.provider ?? 'unknown'), q);
    return out;
  }
  if (!isRecord(body)) return out;
  const src = isRecord(body.providers) ? body.providers : isRecord(body.quotas) ? body.quotas : body;
  for (const [name, v] of Object.entries(src)) {
    if (Array.isArray(v)) v.forEach((q) => push(name, q));
    else if (isRecord(v) && Array.isArray(v.quotas)) v.quotas.forEach((q) => push(name, q));
    else push(name, v);
  }
  return out;
}

/**
 * Accepts hub_api `GET /llm/models` (`{providers: {name: ModelInfo[]}, chain}`), `{provider: ModelInfo[]}`,
 * `{models: {...}}`, `ModelInfo[]` or OpenAI-style `{data: [...]}`.
 */
export function groupModels(body: unknown): Record<string, ModelInfo[]> {
  const out: Record<string, ModelInfo[]> = {};
  const push = (name: string, m: unknown) => {
    if (typeof m === 'string') (out[name] ??= []).push({ id: m, provider: name });
    else if (isRecord(m) && typeof m.id === 'string') (out[name] ??= []).push({ ...(m as unknown as ModelInfo), provider: String(m.provider ?? name) });
  };
  const flat = Array.isArray(body) ? body : isRecord(body) && Array.isArray(body.data) ? body.data : null;
  if (flat) {
    for (const m of flat) push(isRecord(m) ? String(m.provider ?? m.owned_by ?? 'unknown') : 'unknown', m);
    return out;
  }
  if (!isRecord(body)) return out;
  const src = isRecord(body.providers) ? body.providers : isRecord(body.models) ? body.models : body;
  for (const [name, v] of Object.entries(src)) if (Array.isArray(v)) v.forEach((m) => push(name, m));
  return out;
}

/**
 * Accepts the router's `storage_list` shape `{provider: StoredObject[] | "error"}`,
 * `{items|objects|files: StoredObject[], errors: {...}}` or a flat `StoredObject[]`.
 */
export function normalizeListing(body: unknown): DriveListing {
  if (Array.isArray(body)) return { objects: body as StoredObject[], errors: {} };
  const listing: DriveListing = { objects: [], errors: {} };
  if (!isRecord(body)) return listing;
  // hub_api `/drive/files` and `/drive/search` answer `{items, count, errors}`.
  const arr = Array.isArray(body.items)
    ? body.items
    : Array.isArray(body.objects)
      ? body.objects
      : Array.isArray(body.files)
        ? body.files
        : null;
  if (arr) {
    listing.objects = arr as StoredObject[];
    if (isRecord(body.errors)) for (const [k, v] of Object.entries(body.errors)) listing.errors[k] = String(v);
    return listing;
  }
  for (const [name, v] of Object.entries(body)) {
    if (Array.isArray(v)) listing.objects.push(...(v as StoredObject[]).map((o) => ({ ...o, backend: o.backend || name })));
    else listing.errors[name] = typeof v === 'string' ? v : 'error';
  }
  return listing;
}
