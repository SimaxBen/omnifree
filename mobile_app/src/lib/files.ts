// Drive naming and organisation helpers: display names, file kinds, folders (key prefixes), safe names.

import type { StoredObject } from './types';

export type FileKind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'code' | 'sheet' | 'doc' | 'slides' | 'archive' | 'other';

const EXT_KIND: Record<string, FileKind> = {
  jpg: 'image', jpeg: 'image', png: 'image', gif: 'image', webp: 'image', avif: 'image', svg: 'image', bmp: 'image', heic: 'image', heif: 'image', ico: 'image',
  mp4: 'video', webm: 'video', mov: 'video', m4v: 'video', mkv: 'video', '3gp': 'video',
  mp3: 'audio', m4a: 'audio', aac: 'audio', ogg: 'audio', oga: 'audio', opus: 'audio', wav: 'audio', flac: 'audio', amr: 'audio', weba: 'audio',
  pdf: 'pdf',
  txt: 'text', md: 'text', markdown: 'text', log: 'text', rtf: 'text', text: 'text',
  json: 'code', jsonc: 'code', js: 'code', mjs: 'code', cjs: 'code', ts: 'code', tsx: 'code', jsx: 'code', py: 'code', html: 'code', htm: 'code',
  css: 'code', scss: 'code', less: 'code', xml: 'code', yaml: 'code', yml: 'code', toml: 'code', sh: 'code', bash: 'code', zsh: 'code', sql: 'code',
  go: 'code', rs: 'code', java: 'code', kt: 'code', kts: 'code', c: 'code', h: 'code', cpp: 'code', cc: 'code', hpp: 'code', cs: 'code', php: 'code',
  rb: 'code', swift: 'code', ini: 'code', cfg: 'code', conf: 'code', env: 'code', properties: 'code', gradle: 'code', dockerfile: 'code',
  vue: 'code', svelte: 'code', lua: 'code', ps1: 'code', bat: 'code',
  csv: 'sheet', tsv: 'sheet', xls: 'sheet', xlsx: 'sheet', xlsm: 'sheet', xlsb: 'sheet', ods: 'sheet', numbers: 'sheet',
  doc: 'doc', docx: 'doc', odt: 'doc', pages: 'doc',
  ppt: 'slides', pptx: 'slides', odp: 'slides', key: 'slides',
  zip: 'archive', rar: 'archive', '7z': 'archive', tar: 'archive', gz: 'archive', tgz: 'archive', bz2: 'archive', xz: 'archive', zst: 'archive',
};

/** Extension-less files known by their (lower-cased) name. */
const NAME_KIND: Record<string, FileKind> = {
  dockerfile: 'code', makefile: 'code', '.env': 'code', '.gitignore': 'code', '.editorconfig': 'code', license: 'text', readme: 'text',
};

/** Last path segment of a key. */
export function baseName(key: string): string {
  const parts = key.split('/').filter(Boolean);
  return parts[parts.length - 1] ?? key;
}

/** Folder part of a key ('' at the root), without trailing slash. */
export function folderOf(key: string): string {
  const parts = key.split('/').filter(Boolean);
  return parts.slice(0, -1).join('/');
}

export function extensionOf(key: string): string {
  const name = baseName(key);
  const dot = name.lastIndexOf('.');
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : '';
}

/** Name without folder or extension, for titles; the extension is shown separately as a badge. */
export function displayName(key: string): string {
  const name = baseName(key);
  const ext = extensionOf(key);
  return ext ? name.slice(0, -(ext.length + 1)) : name;
}

export function fileKind(key: string, contentType?: string | null): FileKind {
  const byExt = EXT_KIND[extensionOf(key)] ?? NAME_KIND[baseName(key).toLowerCase()];
  if (byExt) return byExt;
  const ct = (contentType ?? '').toLowerCase();
  if (ct.startsWith('image/')) return 'image';
  if (ct.startsWith('video/')) return 'video';
  if (ct.startsWith('audio/')) return 'audio';
  if (ct === 'application/pdf') return 'pdf';
  if (ct.startsWith('text/')) return 'text';
  if (ct.includes('json') || ct.includes('xml') || ct.includes('javascript')) return 'code';
  return 'other';
}

/** Files Ask my Drive can index (hub_api/routers/rag.py). */
export function isIndexable(key: string): boolean {
  return ['pdf', 'txt', 'md', 'csv', 'json', 'html', 'htm', 'docx'].includes(extensionOf(key));
}

/** Previews that read the bytes as text (small files only). */
export function isTextual(kind: FileKind): boolean {
  return kind === 'text' || kind === 'code';
}

/** Which previewer opens a file (components/preview/PreviewBody.tsx). */
export type PreviewerId =
  | 'image'
  | 'video'
  | 'audio'
  | 'pdf'
  | 'docx'
  | 'odt'
  | 'sheet'
  | 'csv'
  | 'pptx'
  | 'zip'
  | 'markdown'
  | 'code'
  | 'text'
  | 'legacy'
  | 'archive'
  | 'none';

const EXT_PREVIEWER: Record<string, PreviewerId> = {
  docx: 'docx', odt: 'odt',
  xlsx: 'sheet', xlsm: 'sheet', xlsb: 'sheet', xls: 'sheet', ods: 'sheet',
  csv: 'csv', tsv: 'csv',
  pptx: 'pptx', zip: 'zip',
  md: 'markdown', markdown: 'markdown',
  doc: 'legacy', ppt: 'legacy',
  rtf: 'none', pages: 'none', numbers: 'none', key: 'none', odp: 'none', heic: 'none', heif: 'none',
};

export function previewerOf(key: string, contentType?: string | null): PreviewerId {
  const byExt = EXT_PREVIEWER[extensionOf(key)];
  if (byExt) return byExt;
  const kind = fileKind(key, contentType);
  if (kind === 'archive') return 'archive';
  if (kind === 'other' || !EXT_KIND[extensionOf(key)]) {
    // No known extension: Office types first (their MIME types contain "xml").
    const ct = (contentType ?? '').toLowerCase();
    if (ct.includes('wordprocessingml')) return 'docx';
    if (ct.includes('spreadsheetml')) return 'sheet';
    if (ct.includes('presentationml')) return 'pptx';
    if (ct === 'application/zip' || ct === 'application/x-zip-compressed') return 'zip';
  }
  if (kind === 'image' || kind === 'video' || kind === 'audio' || kind === 'pdf' || kind === 'code' || kind === 'text') return kind;
  return 'none';
}

const CODE_LANGUAGE: Record<string, string> = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  py: 'python', json: 'json', jsonc: 'json', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', vue: 'xml', svelte: 'xml',
  css: 'css', scss: 'css', less: 'css', sql: 'sql', sh: 'bash', bash: 'bash', zsh: 'bash', makefile: 'bash',
  yaml: 'yaml', yml: 'yaml', toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', env: 'ini', '.env': 'ini', properties: 'ini',
  '.editorconfig': 'ini', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', kts: 'kotlin', gradle: 'kotlin',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp', md: 'markdown', markdown: 'markdown', dockerfile: 'dockerfile',
};

/** highlight.js language of a code file ('' = plain text; auto-detection is never used). */
export function codeLanguage(key: string): string {
  return CODE_LANGUAGE[extensionOf(key) || baseName(key).toLowerCase()] ?? '';
}

/**
 * A safe file name for upload: no folders or path tricks, no control or reserved characters,
 * whitespace collapsed (spaces are kept), extension lower-cased, at most 120 characters.
 */
export function sanitizeFileName(name: string): string {
  let n = name.normalize('NFC');
  n = n.split(/[\\/]/).pop() ?? '';
  // eslint-disable-next-line no-control-regex
  n = n.replace(/[\u0000-\u001f\u007f<>:"|?*#%{}^~[\]`]/g, '').replace(/\s+/g, ' ').trim();
  n = n.replace(/^\.+/, '').replace(/[. ]+$/, '');
  const dot = n.lastIndexOf('.');
  if (dot > 0) n = n.slice(0, dot).trimEnd() + '.' + n.slice(dot + 1).toLowerCase();
  if (n.length > 120) {
    const ext = dot > 0 ? n.slice(n.lastIndexOf('.')) : '';
    n = n.slice(0, 120 - ext.length).trimEnd() + ext;
  }
  return n || 'file';
}

/** A folder path: segments sanitised like names, empty and dot segments dropped. */
export function sanitizeFolder(path: string): string {
  return path
    .split(/[\\/]/)
    .map((s) => s.replace(/[\u0000-\u001f\u007f<>:"|?*#%{}^~[\]`]/g, '').replace(/\s+/g, ' ').trim())
    .filter((s) => s && s !== '.' && s !== '..')
    .join('/');
}

export function joinKey(folder: string, name: string): string {
  const f = sanitizeFolder(folder);
  return f ? `${f}/${name}` : name;
}

export interface FolderEntry {
  name: string;
  path: string;
  count: number;
  size: number;
}

export interface FolderView {
  folders: FolderEntry[];
  files: StoredObject[];
}

/** Splits a flat listing into the sub-folders and files directly inside `prefix`. */
export function folderView(objects: StoredObject[], prefix: string): FolderView {
  const base = sanitizeFolder(prefix);
  const head = base ? `${base}/` : '';
  const folders = new Map<string, FolderEntry>();
  const files: StoredObject[] = [];
  for (const o of objects) {
    const key = o.key.replace(/^\/+/, '');
    if (!key.startsWith(head)) continue;
    const rest = key.slice(head.length);
    const slash = rest.indexOf('/');
    if (slash < 0) {
      if (rest) files.push(o);
      continue;
    }
    const name = rest.slice(0, slash);
    const entry = folders.get(name) ?? { name, path: head + name, count: 0, size: 0 };
    entry.count++;
    entry.size += o.size || 0;
    folders.set(name, entry);
  }
  return { folders: [...folders.values()].sort((a, b) => a.name.localeCompare(b.name)), files };
}

/** Every folder path present in a listing (for the upload folder picker). */
export function allFolders(objects: StoredObject[]): string[] {
  const set = new Set<string>();
  for (const o of objects) {
    const parts = o.key.split('/').filter(Boolean).slice(0, -1);
    for (let i = 1; i <= parts.length; i++) set.add(parts.slice(0, i).join('/'));
  }
  return [...set].sort();
}

export function breadcrumbs(prefix: string): { name: string; path: string }[] {
  const parts = sanitizeFolder(prefix).split('/').filter(Boolean);
  return [{ name: 'Drive', path: '' }, ...parts.map((name, i) => ({ name, path: parts.slice(0, i + 1).join('/') }))];
}

export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const s = Math.round((now.getTime() - t) / 1000);
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  if (d === 1) return 'yesterday';
  if (d < 7) return `${d} days ago`;
  const date = new Date(t);
  return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
}
