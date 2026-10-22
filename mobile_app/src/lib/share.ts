// Android share target: what public/share-sw.js stashed in Cache Storage, and the pure helpers the Share screen
// uses to offer the right actions (note, task, receipt scan, Drive upload, short link, Ask AI, transcription).

export const SHARE_CACHE = 'omnifree-share';
export const SHARE_META = '/__share/meta.json';
export const SHARE_HASH = '#/share';

export interface SharedFileMeta {
  path: string;
  name: string;
  type: string;
  size: number;
}

export interface SharedPayload {
  title: string;
  text: string;
  url: string;
  at: string;
  files: SharedFileMeta[];
}

export type ShareAction = 'receipt' | 'drive' | 'voice' | 'link' | 'note' | 'task' | 'ai';

const URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/i;

/** The first http(s) URL in `text`, without trailing punctuation; '' when there is none. */
export function extractUrl(text: string): string {
  const m = URL_RE.exec(text || '');
  return m ? m[0].replace(/[.,;:!?)\]}»”’]+$/, '') : '';
}

/** The shared link: the `url` field, or (as most Android apps do) a URL inside the text. */
export function sharedUrl(p: Pick<SharedPayload, 'url' | 'text'>): string {
  return (p.url || '').trim() || extractUrl(p.text);
}

/** The shared text without the link (when the link is the whole point, the text is often just the URL again). */
export function sharedText(p: Pick<SharedPayload, 'title' | 'text' | 'url'>): string {
  const url = sharedUrl(p);
  const text = (p.text || '').trim();
  return (url ? text.split(url).join('') : text).replace(/[ \t]+\n/g, '\n').trim();
}

export function isReceiptFile(f: Pick<SharedFileMeta, 'type' | 'name'>): boolean {
  return /^image\//.test(f.type) || f.type === 'application/pdf' || /\.(pdf|jpe?g|png|webp|heic|heif)$/i.test(f.name);
}

export function isAudioFile(f: Pick<SharedFileMeta, 'type' | 'name'>): boolean {
  return /^audio\//.test(f.type) || /\.(m4a|mp3|ogg|opus|wav|webm|aac|amr|3gp)$/i.test(f.name);
}

/** The actions that make sense for a payload, the most likely first. */
export function shareActions(p: SharedPayload): ShareAction[] {
  const out: ShareAction[] = [];
  const hasText = !!(sharedText(p) || p.title.trim());
  const url = sharedUrl(p);
  if (p.files.length === 1 && isAudioFile(p.files[0])) out.push('voice');
  if (p.files.length === 1 && isReceiptFile(p.files[0])) out.push('receipt');
  if (p.files.length) out.push('drive');
  if (url) out.push('link');
  if (hasText || url) out.push('note', 'task', 'ai');
  return out;
}

/** Markdown body for a note: the text, then the link on its own line (not repeated when it is in the text). */
export function noteBodyOf(p: SharedPayload): string {
  const url = sharedUrl(p);
  const text = sharedText(p);
  const title = p.title.trim();
  const parts = [title && !text.startsWith(title) ? `# ${title}` : '', text, url && !text.includes(url) ? url : ''];
  return parts.filter(Boolean).join('\n\n');
}

/** A task title: the shared title, else the first line of the text, else the link (at most 200 characters). */
export function taskTitleOf(p: SharedPayload): string {
  const first = sharedText(p).split('\n').map((l) => l.trim()).find(Boolean) ?? '';
  const title = p.title.trim() || first || sharedUrl(p);
  return title.length > 200 ? `${title.slice(0, 199).trimEnd()}…` : title;
}

/** The prompt sent to Ask AI. */
export function aiPromptOf(p: SharedPayload): string {
  return [p.title.trim(), sharedText(p), sharedUrl(p)].filter(Boolean).join('\n\n');
}

export function parseSharedMeta(body: unknown): SharedPayload | null {
  if (!body || typeof body !== 'object') return null;
  const b = body as Record<string, unknown>;
  const files = Array.isArray(b.files)
    ? (b.files as Record<string, unknown>[])
        .filter((f) => f && typeof f.path === 'string')
        .map((f) => ({ path: String(f.path), name: String(f.name || 'file'), type: String(f.type || 'application/octet-stream'), size: Number(f.size) || 0 }))
    : [];
  const s = (v: unknown) => (typeof v === 'string' ? v : '');
  const payload = { title: s(b.title), text: s(b.text), url: s(b.url), at: s(b.at), files };
  return payload.title || payload.text || payload.url || files.length ? payload : null;
}

// ------------------------------------------------------------------ Cache Storage (browser only)

export async function readShared(): Promise<SharedPayload | null> {
  if (typeof caches === 'undefined') return null;
  const cache = await caches.open(SHARE_CACHE);
  const res = await cache.match(SHARE_META);
  return res ? parseSharedMeta(await res.json()) : null;
}

export async function sharedFile(meta: SharedFileMeta): Promise<File | null> {
  const cache = await caches.open(SHARE_CACHE);
  const res = await cache.match(meta.path);
  if (!res) return null;
  return new File([await res.blob()], meta.name, { type: meta.type });
}

export async function clearShared(): Promise<void> {
  try {
    if (typeof caches !== 'undefined') await caches.delete(SHARE_CACHE);
  } catch {
    // Nothing to clean up.
  }
}
