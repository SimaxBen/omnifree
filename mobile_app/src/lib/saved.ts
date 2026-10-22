// The Saved library (hub /saved): types and the pure helpers the screens use (URL canonicalisation like the hub's
// dedupe, the share-sheet payload -> save request, "site · date" lines, type icons). No React, no I/O.

import type { IconName } from '../components/icons';
import { sharedText, sharedUrl, type SharedPayload } from './share';

export type SavedType =
  | 'article'
  | 'video'
  | 'social post'
  | 'app'
  | 'product'
  | 'recipe'
  | 'place'
  | 'music'
  | 'tool'
  | 'doc'
  | 'image'
  | 'other';

export interface SavedSuggestion {
  name: string;
  id: string | null;
  /** True when the collection does not exist yet (accepting creates it). */
  new: boolean;
}

export interface SavedItem {
  id: string;
  kind: 'url' | 'text' | 'file';
  url: string | null;
  title: string;
  description: string | null;
  site: string;
  site_name: string | null;
  favicon: string | null;
  image_url: string | null;
  /** Presigned URL of the cached thumbnail, or the original image URL. */
  thumb: string | null;
  file: { key: string; provider: string | null } | null;
  mime: string | null;
  author: string | null;
  published_at: string | null;
  type: SavedType;
  summary: string | null;
  tags: string[];
  note: string | null;
  collection_id: string | null;
  suggestion: SavedSuggestion | null;
  enrich_status: 'ok' | 'partial' | 'pending';
  enrich_error: string | null;
  ai_source: 'llm' | 'heuristic' | null;
  indexed: boolean;
  saved_at: string;
  created_at: string;
  updated_at: string;
  /** Detail only: the start of the page text. */
  text?: string;
}

export interface SavedCollection {
  id: string;
  name: string;
  color: string | null;
  emoji: string | null;
  position: number;
  count: number;
}

export interface SmartCollection {
  type: SavedType;
  label: string;
  count: number;
}

export interface SavedCollections {
  collections: SavedCollection[];
  uncategorised: number;
  smart: SmartCollection[];
  total: number;
}

export interface SavedListing {
  items: SavedItem[];
  total: number;
  next_offset: number | null;
}

export interface SaveRequest {
  url?: string;
  text?: string;
  key?: string;
  provider?: string;
  title?: string;
  note?: string;
  collection_id?: string;
  collection?: string;
}

export interface SaveResult {
  item: SavedItem;
  duplicate: boolean;
}

export interface SavedPatch {
  title?: string;
  note?: string | null;
  tags?: string[];
  /** '' = Uncategorised. */
  collection_id?: string;
  /** Move into this collection by name (created when missing): how a suggestion is accepted. */
  collection?: string;
  type?: SavedType;
}

export interface SavedPreview {
  url: string;
  canonical: string;
  site: string;
  title: string | null;
  description: string | null;
  image: string | null;
  site_name: string | null;
  favicon: string | null;
  status: 'ok' | 'partial' | 'pending';
  type: SavedType;
}

export interface SavedPreviewResult {
  preview: SavedPreview | null;
  existing: SavedItem | null;
}

export interface SavedSearchResult {
  item: SavedItem;
  score: number;
  match: 'both' | 'keyword' | 'meaning' | 'type';
  reason: string;
  snippet: string;
  similarity: number | null;
}

export interface SavedSearchResponse {
  query: string;
  terms: string[];
  type_hint: SavedType | null;
  results: SavedSearchResult[];
  mode: 'hybrid' | 'keyword';
  reranked: boolean;
}

export interface SavedSource {
  n: number;
  id: string;
  title: string;
  url: string | null;
  site: string;
  type: SavedType;
  saved_at: string | null;
  thumb: string | null;
  snippet: string;
}

export interface SavedAnswer {
  answer: string;
  sources: SavedSource[];
  routed_via: string;
  period: { start: string; end: string; key: string } | null;
}

export interface CollectionBody {
  name?: string;
  color?: string;
  emoji?: string;
  position?: number;
}

// ------------------------------------------------------------------ type icons and labels

export const TYPE_ICON: Record<SavedType, IconName> = {
  article: 'book',
  video: 'play',
  'social post': 'chat',
  app: 'grid',
  product: 'tag',
  recipe: 'receipt',
  place: 'pin',
  music: 'wave',
  tool: 'bolt',
  doc: 'file',
  image: 'camera',
  other: 'bookmark',
};

export const TYPE_LABEL: Record<SavedType, string> = {
  article: 'Article',
  video: 'Video',
  'social post': 'Post',
  app: 'App',
  product: 'Product',
  recipe: 'Recipe',
  place: 'Place',
  music: 'Music',
  tool: 'Tool',
  doc: 'Doc',
  image: 'Image',
  other: 'Other',
};

export const COLLECTION_COLORS = ['#FFC24B', '#5EE0A8', '#7FA8FF', '#FF7A9C', '#C9A7FF', '#FF9548', '#4FD1E8', '#B8E06B'];

// ------------------------------------------------------------------ canonical URLs (same rules as the hub's dedupe)

const TRACKING = new Set([
  'fbclid', 'gclid', 'dclid', 'gbraid', 'wbraid', 'msclkid', 'yclid', 'mc_cid', 'mc_eid', 'igshid', 'igsh', 'ref_src',
  'ref_url', '_hsenc', '_hsmi', 'mkt_tok', 'oly_anon_id', 'oly_enc_id', 'vero_id', 'spm', 'si', 'feature', 'share',
  'share_id', 'ref', 'utm', 'rcm', 'smid', 'smtyp', 'cmpid', 's_cid', 'wt_mc', '_ga', '_gl', 'trk', 'trkcampaign',
  'sr_share', 'ved', 'usg', 'ei', 'sxsrf', 'pp', 'ab_channel',
]);
const X_HOSTS = new Set(['twitter.com', 'x.com', 'mobile.twitter.com', 'mobile.x.com', 'fxtwitter.com', 'vxtwitter.com', 'fixupx.com', 'fixvx.com', 'nitter.net']);
const YT_HOSTS = new Set(['youtube.com', 'music.youtube.com', 'youtube-nocookie.com', 'youtu.be']);
const YT_ID = /^[A-Za-z0-9_-]{11}$/;

function bareHost(host: string): string {
  let h = host.toLowerCase().replace(/\.+$/, '');
  for (const prefix of ['www.', 'm.', 'mobile.']) {
    if (h.startsWith(prefix) && h.split('.').length >= 3) h = h.slice(prefix.length);
  }
  return h;
}

function isTracking(name: string): boolean {
  const low = name.toLowerCase();
  return low.startsWith('utm_') || low.startsWith('pk_') || low.startsWith('hsa_') || TRACKING.has(low);
}

function parseUrl(raw: string): URL | null {
  const value = (raw || '').trim();
  if (!/^https?:\/\//i.test(value)) return null;
  try {
    const u = new URL(value);
    return u.hostname ? u : null;
  } catch {
    return null;
  }
}

/** The 11-character id of a YouTube link (watch, youtu.be, shorts, embed, live), else null. */
export function youtubeId(raw: string): string | null {
  const u = parseUrl(raw);
  if (!u) return null;
  const host = bareHost(u.hostname);
  if (!YT_HOSTS.has(host)) return null;
  if (host === 'youtu.be') {
    const id = u.pathname.split('/').filter(Boolean)[0] ?? '';
    return YT_ID.test(id) ? id : null;
  }
  const v = u.searchParams.get('v') ?? '';
  if (YT_ID.test(v)) return v;
  const segs = u.pathname.split('/').filter(Boolean);
  return segs.length >= 2 && ['shorts', 'embed', 'live', 'v'].includes(segs[0]) && YT_ID.test(segs[1]) ? segs[1] : null;
}

/** A stable form of a link for "already saved?" checks; null when it is not an http(s) URL. */
export function canonicalUrl(raw: string): string | null {
  const u = parseUrl(raw);
  if (!u) return null;
  const vid = youtubeId(raw);
  if (vid) return `https://youtube.com/watch?v=${vid}`;
  let host = bareHost(u.hostname);
  if (X_HOSTS.has(host)) {
    const m = /\/status(?:es)?\/(\d{1,25})/.exec(u.pathname);
    if (m) return `https://x.com/${(u.pathname.split('/').filter(Boolean)[0] || 'i').toLowerCase()}/status/${m[1]}`;
    host = 'x.com';
  }
  const port = u.port && u.port !== '80' && u.port !== '443' ? `:${u.port}` : '';
  let path = u.pathname.replace(/\/{2,}/g, '/') || '/';
  if (path.length > 1) path = path.replace(/\/+$/, '') || '/';
  let params = [...u.searchParams.entries()].filter(([k, v]) => v !== '' && !isTracking(k));
  if (host === 'play.google.com') params = params.filter(([k]) => k === 'id');
  if (host === 'x.com') params = params.filter(([k]) => k !== 's' && k !== 't');
  params.sort(([a, av], [b, bv]) => (a === b ? (av < bv ? -1 : av > bv ? 1 : 0) : a < b ? -1 : 1));
  const query = params.length ? `?${new URLSearchParams(params).toString()}` : '';
  return `https://${host}${port}${path}${query}`;
}

/** `youtube.com`, `x.com`, `play.google.com`: the host without www. ('' for anything else). */
export function siteOf(raw: string | null | undefined): string {
  const u = parseUrl(raw ?? '');
  if (!u) return '';
  const host = bareHost(u.hostname);
  if (host === 'youtu.be') return 'youtube.com';
  if (X_HOSTS.has(host)) return 'x.com';
  return host;
}

// ------------------------------------------------------------------ share sheet -> save request

/** What one tap on "Save" sends for text / a link shared from another app; null when there is nothing to save. */
export function saveRequestFromShare(p: Pick<SharedPayload, 'title' | 'text' | 'url'>): SaveRequest | null {
  const url = sharedUrl(p);
  const text = sharedText(p);
  const title = (p.title || '').trim();
  if (url) {
    const req: SaveRequest = { url };
    // "Title\nhttps://…" (Chrome, YouTube, news apps): a short single line is the title, anything longer a note.
    const extra = text && text !== title ? text : '';
    const hint = title && !parseUrl(title) ? title : !extra.includes('\n') && extra.length <= 200 ? extra : '';
    if (hint) req.title = hint.slice(0, 300);
    const note = extra && extra !== hint ? extra : '';
    if (note) req.note = note.slice(0, 4000);
    return req;
  }
  const body = [title && !text.startsWith(title) ? title : '', text].filter(Boolean).join('\n\n').trim();
  return body ? { text: body.slice(0, 20000), ...(title ? { title: title.slice(0, 300) } : {}) } : null;
}

/** Drive key for a file shared to Save: `saved/files/YYYY/MM/<timestamp>-<safe name>`. */
export function savedFileKey(name: string, now: Date = new Date()): string {
  const safe = (name || 'file').replace(/[^\w.\- ]+/g, '_').replace(/\s+/g, '-').slice(-80) || 'file';
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  return `saved/files/${now.getFullYear()}/${mm}/${now.getTime()}-${safe}`;
}

// ------------------------------------------------------------------ display

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** "Today", "Yesterday", "21 Oct" (this year) or "21 Oct 2025", in the device's time zone. */
export function savedDate(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  if (dayKey(d) === dayKey(now)) return 'Today';
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (dayKey(d) === dayKey(y)) return 'Yesterday';
  const base = `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return d.getFullYear() === now.getFullYear() ? base : `${base} ${d.getFullYear()}`;
}

/** The source of an item: its site, else "Note" for text and "Drive" for files. */
export function sourceLabel(item: Pick<SavedItem, 'site' | 'kind' | 'url'>): string {
  if (item.site) return item.site;
  if (item.kind === 'text') return 'Note';
  if (item.kind === 'file') return 'Drive';
  return siteOf(item.url) || 'Link';
}

/** "youtube.com · Today": the line under a saved item's title. */
export function siteDateLine(item: Pick<SavedItem, 'site' | 'kind' | 'url' | 'saved_at'>, now: Date = new Date()): string {
  return [sourceLabel(item), savedDate(item.saved_at, now)].filter(Boolean).join(' · ');
}

/** Collection chips: every collection with items first (their order), then the empty ones. */
export function sortCollections(cols: SavedCollection[]): SavedCollection[] {
  return [...cols].sort((a, b) => Number(b.count > 0) - Number(a.count > 0) || a.position - b.position || a.name.localeCompare(b.name));
}

/** True when `text` looks like a single link (the Save sheet previews it). */
export function looksLikeUrl(text: string): boolean {
  return /^https?:\/\/\S+$/i.test(text.trim());
}
