// Where previewers get file bytes from. A Drive file is read straight from its storage backend
// through a presigned URL (needs the bucket's CORS rule for this origin); when the browser may not
// read it (CORS) small files come through the hub instead (its serverless body limit is ~4.5 MB).
// Entries opened inside a zip are in-memory sources with the same interface.

import { HubError, type HubClient } from '../api';
import { baseName } from '../files';
import type { StoredObject } from '../types';

/** Largest file fetched through the hub when storage CORS blocks a direct read. */
export const HUB_READ_LIMIT = 4 * 1024 * 1024;

export class TooBigError extends Error {
  constructor(
    public size: number,
    public limit: number,
  ) {
    super('Too large to preview');
  }
}

export class CorsBlockedError extends Error {
  constructor() {
    super("This storage backend doesn't let the app read the file directly (CORS). Download it instead.");
  }
}

export interface PreviewSource {
  /** Stable identity for effects and caches. */
  id: string;
  name: string;
  /** Key or path inside an archive: decides the previewer. */
  path: string;
  size: number | null;
  contentType?: string | null;
  /** The Drive object, for top-level files (not archive entries). */
  obj?: StoredObject;
  /** A URL for <img>/<video>/<audio>: presigned or blob:. */
  url(signal?: AbortSignal): Promise<string>;
  /** The whole file; rejects with TooBigError past `max` bytes. */
  bytes(max: number, signal?: AbortSignal): Promise<ArrayBuffer>;
  /** Save a copy on the device (or open it in a new tab). */
  download(): Promise<void>;
}

// ------------------------------------------------------------------ presigned URLs

const urlCache = new Map<string, { url: string; until: number }>();

/**
 * A URL that serves `obj` without the hub: a presigned GET when the backend supports it (cached
 * for most of its lifetime), else a blob URL downloaded through the hub (small files only).
 */
export async function objectUrl(client: HubClient, obj: StoredObject, signal?: AbortSignal): Promise<string> {
  const k = `${obj.backend}/${obj.key}`;
  const hit = urlCache.get(k);
  if (hit && hit.until > Date.now()) return hit.url;
  try {
    const res = await client.drivePresign(obj.key, obj.backend, signal);
    if (res.url) {
      urlCache.set(k, { url: res.url, until: Date.now() + Math.max((res.expires_s ?? 3600) - 120, 60) * 1000 });
      return res.url;
    }
  } catch (err) {
    if (!(err instanceof HubError && err.status >= 400 && err.status < 500)) throw err;
  }
  const blob = await client.driveDownload(obj.key, obj.backend);
  const url = URL.createObjectURL(blob);
  urlCache.set(k, { url, until: Date.now() + 30 * 60_000 });
  return url;
}

/** Backends whose bucket refused a cross-origin read this session (no CORS rule for this origin). */
const corsBlocked = new Set<string>();

export function isCorsBlocked(backend: string): boolean {
  return corsBlocked.has(backend);
}

async function viaHub(client: HubClient, obj: StoredObject): Promise<ArrayBuffer> {
  return (await client.driveDownload(obj.key, obj.backend)).arrayBuffer();
}

/** The bytes of a Drive file (whole), direct from storage when possible. */
export async function fetchObjectBytes(client: HubClient, obj: StoredObject, max: number, signal?: AbortSignal): Promise<ArrayBuffer> {
  if (obj.size > max) throw new TooBigError(obj.size, max);
  if (corsBlocked.has(obj.backend) && obj.size <= HUB_READ_LIMIT) return viaHub(client, obj);
  const url = await objectUrl(client, obj, signal);
  let res: Response;
  try {
    res = await fetch(url, { signal, credentials: 'omit', referrerPolicy: 'no-referrer' });
  } catch (err) {
    if (signal?.aborted) throw err;
    // A TypeError here is (almost always) the bucket's CORS rule refusing this origin.
    corsBlocked.add(obj.backend);
    if (obj.size <= HUB_READ_LIMIT) return viaHub(client, obj);
    throw new CorsBlockedError();
  }
  if (!res.ok) throw new Error(`Storage answered ${res.status}`);
  const len = Number(res.headers.get('content-length') ?? 0);
  if (len > max) {
    void res.body?.cancel();
    throw new TooBigError(len, max);
  }
  return res.arrayBuffer();
}

/**
 * Bytes `start..end` (inclusive) of a Drive file through an HTTP Range request. When the range is
 * ignored (200, or the hub/blob fallback) the whole file is answered in `whole`, but only when it
 * is at most `wholeMax` bytes; callers keep it rather than asking again.
 */
export async function fetchObjectRange(
  client: HubClient,
  obj: StoredObject,
  start: number,
  end: number,
  wholeMax: number,
  signal?: AbortSignal,
): Promise<{ data: ArrayBuffer; whole: ArrayBuffer | null }> {
  const all = (buf: ArrayBuffer) => ({ data: buf.slice(start, end + 1), whole: buf });
  if (corsBlocked.has(obj.backend)) {
    if (obj.size > Math.min(wholeMax, HUB_READ_LIMIT)) throw new CorsBlockedError();
    return all(await viaHub(client, obj));
  }
  const url = await objectUrl(client, obj, signal);
  if (url.startsWith('blob:')) return all(await (await fetch(url)).arrayBuffer());
  let res: Response;
  try {
    res = await fetch(url, { signal, credentials: 'omit', referrerPolicy: 'no-referrer', headers: { Range: `bytes=${start}-${end}` } });
  } catch (err) {
    if (signal?.aborted) throw err;
    corsBlocked.add(obj.backend);
    throw new CorsBlockedError();
  }
  if (res.status === 206) return { data: await res.arrayBuffer(), whole: null };
  if (!res.ok) throw new Error(`Storage answered ${res.status}`);
  if (obj.size > wholeMax) {
    void res.body?.cancel();
    throw new TooBigError(obj.size, wholeMax);
  }
  return all(await res.arrayBuffer());
}

// ------------------------------------------------------------------ saving

function clickDownload(href: string, name: string, newTab: boolean): void {
  const a = document.createElement('a');
  a.href = href;
  a.download = name;
  a.rel = 'noopener';
  if (newTab) a.target = '_blank';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Opens the file in a new tab (the browser or an app then shows or saves it). */
export async function openObjectExternally(client: HubClient, obj: StoredObject): Promise<void> {
  let tab: Window | null = null;
  try {
    tab = window.open('', '_blank'); // opened in the tap's gesture, before the await (popup blockers)
  } catch {
    tab = null;
  }
  try {
    const url = await objectUrl(client, obj);
    if (tab) {
      tab.opener = null;
      tab.location.href = url;
    } else {
      clickDownload(url, baseName(obj.key), true);
    }
  } catch (err) {
    tab?.close();
    throw err;
  }
}

export function saveBytes(bytes: BlobPart, name: string, type = 'application/octet-stream'): void {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  clickDownload(url, name, false);
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// ------------------------------------------------------------------ sources

export function driveSource(client: HubClient, obj: StoredObject): PreviewSource {
  return {
    id: `${obj.backend}/${obj.key}`,
    name: baseName(obj.key),
    path: obj.key,
    size: obj.size,
    contentType: obj.content_type,
    obj,
    url: (signal) => objectUrl(client, obj, signal),
    bytes: (max, signal) => fetchObjectBytes(client, obj, max, signal),
    download: () => openObjectExternally(client, obj),
  };
}

/** An in-memory file (an entry extracted from an archive). Call `dispose` when done with it. */
export function memorySource(id: string, path: string, data: Uint8Array, contentType: string): PreviewSource & { dispose: () => void } {
  let url: string | null = null;
  const name = path.split('/').filter(Boolean).pop() ?? path;
  const blob = () => new Blob([data as BlobPart], { type: contentType });
  return {
    id,
    name,
    path,
    size: data.byteLength,
    contentType,
    url: async () => (url ??= URL.createObjectURL(blob())),
    bytes: async (max) => {
      if (data.byteLength > max) throw new TooBigError(data.byteLength, max);
      return data.slice().buffer as ArrayBuffer;
    },
    download: async () => saveBytes(data as BlobPart, name, contentType),
    dispose: () => {
      if (url) URL.revokeObjectURL(url);
      url = null;
    },
  };
}

/** Best-effort MIME type from a file name (archive entries carry none). */
export function mimeOf(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  const map: Record<string, string> = {
    png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', avif: 'image/avif', bmp: 'image/bmp',
    svg: 'image/svg+xml', ico: 'image/x-icon', pdf: 'application/pdf', mp4: 'video/mp4', webm: 'video/webm', mov: 'video/quicktime',
    mp3: 'audio/mpeg', m4a: 'audio/mp4', ogg: 'audio/ogg', opus: 'audio/ogg', wav: 'audio/wav', flac: 'audio/flac', aac: 'audio/aac',
    txt: 'text/plain', md: 'text/markdown', csv: 'text/csv', json: 'application/json',
  };
  return map[ext] ?? 'application/octet-stream';
}
