// Offline mode, pure part: which requests are cached or queued, optimistic objects, the overlay of
// queued writes on lists, queue coalescing and how a replayed response is classified.
// No I/O here: everything is exercised by src/lib/__tests__/offline-model.test.ts.

export type Kind = 'task' | 'note' | 'expense' | 'income';
export type OpKind = Kind | 'voice';
export type Op = 'create' | 'update' | 'delete' | 'upload';
export type Method = 'POST' | 'PATCH' | 'DELETE';

/** A voice recording captured offline, uploaded later. */
export interface QueuedUpload {
  data: ArrayBuffer;
  type: string;
  filename: string;
  /** The other multipart fields (mode, notebook, kind, idempotency_key...). */
  fields: Record<string, string>;
}

export interface QueueItem {
  /** IndexedDB key, in enqueue order. */
  seq?: number;
  kind: OpKind;
  op: Op;
  method: Method;
  path: string;
  /** The row id (client-generated for creates) or, for uploads, the idempotency key. */
  resourceId: string;
  body?: Record<string, unknown>;
  upload?: QueuedUpload;
  createdAt: number;
  status: 'pending' | 'conflict';
  attempts: number;
  httpStatus?: number;
  error?: string;
  label: string;
}

export interface Mutation {
  kind: OpKind;
  op: Op;
  method: Method;
  /** Row id for update/delete. */
  resourceId?: string;
}

/** Marker on objects shown optimistically until the queue syncs them. */
export const PENDING = '_pending';

const COLLECTIONS: Record<Kind, string> = { task: '/tasks', note: '/notes', expense: '/expenses', income: '/money/incomes' };
const LIST_FIELD: Record<Kind, string> = { task: 'tasks', note: 'notes', expense: 'expenses', income: 'incomes' };
const KINDS = Object.keys(COLLECTIONS) as Kind[];
/** Sub-routes of a collection that are not row ids. */
const NOT_IDS = new Set(['voice', 'receipt', 'overview', 'fx', 'incomes', 'recurring', 'accounts', 'budgets', 'sms']);
const VOICE_PATHS = new Set(['/notes/voice', '/money/voice']);

const CACHEABLE: RegExp[] = [
  /^\/tasks$/,
  /^\/notes$/,
  /^\/notes\/[^/]+$/,
  /^\/expenses$/,
  /^\/money\/overview$/,
  /^\/money\/incomes$/,
  /^\/gcal\/events$/,
  /^\/chats$/,
  /^\/chats\/[^/]+$/,
];

export function isCacheableGet(path: string): boolean {
  if (path === '/notes/voice') return false;
  return CACHEABLE.some((re) => re.test(path));
}

type QueryValue = string | number | boolean | null | undefined;

/** Stable cache key: the path plus the non-empty query values, sorted (same filter as HubClient.url). */
export function cacheKey(path: string, query?: Record<string, QueryValue>): string {
  const parts = Object.entries(query ?? {})
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .sort();
  return parts.length ? `${path}?${parts.join('&')}` : path;
}

/** The queued write a request stands for, or null when it is not one the offline queue handles. */
export function matchMutation(method: string, path: string): Mutation | null {
  const m = method.toUpperCase();
  if (m === 'POST' && VOICE_PATHS.has(path)) return { kind: 'voice', op: 'upload', method: 'POST' };
  for (const kind of KINDS) {
    const base = COLLECTIONS[kind];
    if (m === 'POST' && path === base) return { kind, op: 'create', method: 'POST' };
    if ((m === 'PATCH' || m === 'DELETE') && path.startsWith(`${base}/`)) {
      const rest = path.slice(base.length + 1);
      if (!rest || rest.includes('/')) continue;
      const id = decodeURIComponent(rest);
      if (NOT_IDS.has(id)) continue;
      return { kind, op: m === 'PATCH' ? 'update' : 'delete', method: m, resourceId: id };
    }
  }
  return null;
}

export function collectionPath(kind: Kind): string {
  return COLLECTIONS[kind];
}

export function rowPath(kind: Kind, id: string): string {
  return `${COLLECTIONS[kind]}/${encodeURIComponent(id)}`;
}

const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';

/** A client-generated row id the hub accepts (8-64 of A-Z a-z 0-9 _ -): "o" + 15 random base62 characters. */
export function newClientId(): string {
  const bytes = new Uint8Array(15);
  globalThis.crypto.getRandomValues(bytes);
  return `o${Array.from(bytes, (b) => BASE62[b % 62]).join('')}`;
}

function localDay(now: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
}

function deriveTitle(body: string): string {
  for (const line of body.split('\n')) {
    const text = line.replace(/^(?:\s*(?:#+|[-*+]|\d+[.)]|>|\[[ xX]\])\s*)+/, '').replace(/[*_`~]+/g, '').trim();
    if (text) return text.length <= 80 ? text : `${text.slice(0, 79).trimEnd()}…`;
  }
  return 'Untitled';
}

type Obj = Record<string, unknown>;

/** What the hub would answer for a create, built locally (same defaults as the routers). */
export function optimisticCreate(kind: Kind, id: string, body: Obj, createdAt: number): Obj {
  const at = new Date(createdAt);
  const iso = at.toISOString().replace(/\.\d{3}Z$/, 'Z');
  switch (kind) {
    case 'task':
      return {
        details: null, due: null, due_time: null, priority: 'normal', note_id: null, done_at: null,
        ...body, id, title: String(body.title ?? '').trim(), done: false, created_at: iso, [PENDING]: true,
      };
    case 'note': {
      const text = String(body.body ?? '');
      const title = String(body.title ?? '').trim() || deriveTitle(text);
      return {
        tags: [], pinned: false, source: 'text', audio_key: null,
        ...body, id, body: text, title, notebook: String(body.notebook ?? '').trim() || 'Inbox',
        created_at: iso, updated_at: iso, [PENDING]: true,
      };
    }
    case 'expense':
      return {
        merchant: '', currency: 'MAD', category: 'other', items: [], note: null, receipt_key: null, source: null, source_ref: null,
        ...body, id, date: (body.date as string) || localDay(at), total: Number(body.total ?? 0), created_at: iso, [PENDING]: true,
      };
    case 'income':
      return {
        currency: 'MAD', source: '', category: 'other', note: null, recurring: false, end_month: null, origin: 'manual', audio_key: null,
        ...body, id, date: (body.date as string) || localDay(at), amount: Number(body.amount ?? 0), created_at: iso, [PENDING]: true,
      };
  }
}

/** ``base`` with ``patch`` applied, the way the hub would answer the PATCH. */
export function optimisticUpdate(kind: Kind, base: Obj, patch: Obj, at: number): Obj {
  const out: Obj = { ...base, ...patch, [PENDING]: true };
  if (kind === 'task' && 'done' in patch) {
    out.done_at = patch.done ? (base.done ? base.done_at ?? null : new Date(at).toISOString()) : null;
  }
  if (kind === 'task' && 'due' in patch && !patch.due) out.due_time = null;
  if (kind === 'note') out.updated_at = new Date(at).toISOString().replace(/\.\d{3}Z$/, 'Z');
  return out;
}

export function isPending(obj: unknown): boolean {
  return !!obj && typeof obj === 'object' && (obj as Obj)[PENDING] === true;
}

function sum(rows: Obj[]): Obj {
  const byCurrency: Record<string, number> = {};
  const byCategory: Record<string, Record<string, number>> = {};
  for (const e of rows) {
    const cur = String(e.currency ?? 'MAD');
    const cat = String(e.category ?? 'other');
    const amount = Number(e.total ?? 0);
    byCurrency[cur] = (byCurrency[cur] ?? 0) + amount;
    const bucket = (byCategory[cat] ??= {});
    bucket[cur] = (bucket[cur] ?? 0) + amount;
  }
  const r = (n: number) => Math.round(n * 100) / 100;
  return {
    total_by_currency: Object.fromEntries(Object.entries(byCurrency).map(([k, v]) => [k, r(v)])),
    by_category: Object.fromEntries(Object.entries(byCategory).map(([c, m]) => [c, Object.fromEntries(Object.entries(m).map(([k, v]) => [k, r(v)]))])),
    count: rows.length,
  };
}

function kindOfList(path: string): Kind | null {
  for (const kind of KINDS) if (COLLECTIONS[kind] === path) return kind;
  return null;
}

/** Whether a created row belongs in the list asked for with ``query``. */
function accepts(kind: Kind, obj: Obj, query: Record<string, QueryValue>, today: string): boolean {
  if (kind === 'task') {
    const status = String(query.status ?? 'open');
    return status === 'all' || (status === 'done') === !!obj.done;
  }
  if (kind === 'note') return !query.q && (!query.notebook || obj.notebook === query.notebook);
  const month = String(query.month ?? (kind === 'expense' ? today.slice(0, 7) : ''));
  if (!month) return true;
  const date = String(obj.date ?? '');
  return date.startsWith(month) || (kind === 'income' && !!obj.recurring && date.slice(0, 7) <= month);
}

/**
 * A GET body with the pending queue applied: queued creates added (when they match the query),
 * updates merged, deletes removed. Bodies it does not know are returned unchanged.
 */
export function overlay(path: string, query: Record<string, QueryValue> | undefined, body: unknown, items: QueueItem[], now = Date.now()): unknown {
  const pending = items.filter((i) => i.status === 'pending' && i.kind !== 'voice');
  if (!pending.length || !body || typeof body !== 'object') return body;
  const q = query ?? {};

  const single = /^\/notes\/([^/]+)$/.exec(path);
  if (single) {
    const id = decodeURIComponent(single[1]);
    let out = body as Obj;
    for (const it of pending) {
      if (it.kind === 'note' && it.op === 'update' && it.resourceId === id) out = optimisticUpdate('note', out, it.body ?? {}, it.createdAt);
    }
    return out;
  }

  const kind = kindOfList(path);
  if (!kind) return body;
  const field = LIST_FIELD[kind];
  const src = body as Obj;
  if (!Array.isArray(src[field])) return body;
  let list = (src[field] as Obj[]).slice();
  let changed = false;
  const today = localDay(new Date(now));
  for (const it of pending) {
    if (it.kind !== kind) continue;
    if (it.op === 'create') {
      if (list.some((x) => x.id === it.resourceId)) continue; // synced already, not yet removed
      const obj = optimisticCreate(kind, it.resourceId, it.body ?? {}, it.createdAt);
      if (!accepts(kind, obj, q, today)) continue;
      list = kind === 'note' ? [obj, ...list] : [...list, obj];
      changed = true;
    } else if (it.op === 'update') {
      list = list.map((x) => {
        if (x.id !== it.resourceId) return x;
        changed = true;
        return optimisticUpdate(kind, x, it.body ?? {}, it.createdAt);
      });
    } else if (it.op === 'delete') {
      const before = list.length;
      list = list.filter((x) => x.id !== it.resourceId);
      changed ||= list.length !== before;
    }
  }
  if (!changed) return body;
  if (kind === 'task') list = list.filter((x) => accepts('task', x, q, today));
  const out: Obj = { ...src, [field]: list };
  if (kind === 'expense' && src.summary && typeof src.summary === 'object') out.summary = sum(list);
  return out;
}

/** A queued create of row ``id``, if any (to answer GET /notes/{id} before the hub knows it). */
export function pendingCreate(items: QueueItem[], kind: Kind, id: string): Obj | null {
  let out: Obj | null = null;
  for (const it of items) {
    if (it.status !== 'pending' || it.kind !== kind || it.resourceId !== id) continue;
    if (it.op === 'create') out = optimisticCreate(kind, id, it.body ?? {}, it.createdAt);
    else if (it.op === 'update' && out) out = optimisticUpdate(kind, out, it.body ?? {}, it.createdAt);
    else if (it.op === 'delete') out = null;
  }
  return out;
}

export interface EnqueuePlan {
  /** Queue entries (seq) made useless by the new write. */
  drop: number[];
  /** An existing entry rewritten to absorb the new write. */
  update?: QueueItem;
  /** The new write, when it is queued as its own entry. */
  add?: QueueItem;
}

/**
 * Coalescing: an update folds into the queued create (or the last queued update) of the same row;
 * deleting a row whose create is still queued drops both, and nothing is sent at all.
 */
export function planEnqueue(queue: QueueItem[], next: QueueItem): EnqueuePlan {
  const same = queue.filter((i) => i.status === 'pending' && i.kind === next.kind && i.resourceId === next.resourceId && i.seq !== undefined);
  if (next.op === 'update') {
    const last = same[same.length - 1];
    if (last && (last.op === 'create' || last.op === 'update')) {
      return { drop: [], update: { ...last, body: { ...(last.body ?? {}), ...(next.body ?? {}) }, label: last.op === 'create' ? last.label : next.label } };
    }
    return { drop: [], add: next };
  }
  if (next.op === 'delete') {
    const created = same.some((i) => i.op === 'create');
    const drop = same.filter((i) => i.op === 'create' || i.op === 'update').map((i) => i.seq as number);
    return created ? { drop } : { drop, add: next };
  }
  return { drop: [], add: next };
}

export type Outcome = 'done' | 'conflict' | 'retry' | 'stop';

/** What a replayed response means: synced, a conflict to show, try again later, or stop (auth). */
export function classify(method: Method, status: number): Outcome {
  if (status >= 200 && status < 300) return 'done';
  if (method === 'DELETE' && (status === 404 || status === 410)) return 'done'; // already gone
  if (status === 0 || status === 408 || status === 425 || status === 429 || status >= 500) return 'retry';
  if (status === 401 || status === 403) return 'stop';
  return 'conflict';
}

/** After this many 5xx answers an entry becomes a conflict instead of blocking the queue forever. */
export const MAX_SERVER_ERRORS = 5;

const NOUN: Record<OpKind, string> = { task: 'task', note: 'note', expense: 'expense', income: 'income', voice: 'voice recording' };

export function labelFor(kind: OpKind, op: Op, body?: Obj, base?: Obj | null): string {
  const src = { ...(base ?? {}), ...(body ?? {}) };
  const name =
    kind === 'task' ? src.title
    : kind === 'note' ? src.title || (typeof src.body === 'string' ? deriveTitle(src.body) : '')
    : kind === 'expense' ? [src.merchant, src.total !== undefined ? `${src.total} ${src.currency ?? 'MAD'}` : ''].filter(Boolean).join(' ')
    : kind === 'income' ? [src.source, src.amount !== undefined ? `${src.amount} ${src.currency ?? 'MAD'}` : ''].filter(Boolean).join(' ')
    : '';
  const what = String(name ?? '').trim();
  if (kind === 'task' && op === 'update' && Object.keys(body ?? {}).length === 1 && 'done' in (body ?? {})) {
    return `${body?.done ? 'Complete' : 'Reopen'} task${what ? ` "${what}"` : ''}`;
  }
  const verb = { create: 'New', update: 'Edit', delete: 'Delete', upload: 'Upload' }[op];
  return `${verb} ${NOUN[kind]}${what ? ` "${what.length > 48 ? `${what.slice(0, 47)}…` : what}"` : ''}`;
}

export function detailOf(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === 'object' && 'msg' in x ? String((x as { msg: unknown }).msg) : JSON.stringify(x))).join('; ');
  }
  if (typeof body === 'string' && body) return body.slice(0, 200);
  if (status === 0) return 'Hub unreachable';
  return `HTTP ${status}`;
}

/** "HH:MM" of a timestamp, for the offline banner. */
export function clockOf(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export interface BannerInput {
  online: boolean;
  cachedAt: number | null;
  pending: number;
  conflicts: number;
  syncing: boolean;
}

/** The offline banner's line and tone, or null when everything is live and synced. */
export function bannerText(s: BannerInput): { text: string; tone: 'offline' | 'sync' | 'conflict' } | null {
  const parts: string[] = [];
  if (s.cachedAt !== null) parts.push(`Offline — showing saved data from ${clockOf(s.cachedAt)}`);
  else if (!s.online) parts.push('Offline — changes sync when you are back online');
  if (s.pending) parts.push(s.syncing ? `syncing ${s.pending}…` : `${s.pending} pending sync`);
  if (s.conflicts) parts.push(`${s.conflicts} need${s.conflicts === 1 ? 's' : ''} attention`);
  if (!parts.length) return null;
  const tone = s.conflicts ? 'conflict' : s.cachedAt !== null || !s.online ? 'offline' : 'sync';
  return { text: parts.join(' · '), tone };
}
