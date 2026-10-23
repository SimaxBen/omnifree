// Zip archives in the previewer: a capped listing (entry count, declared uncompressed size), folder
// navigation inside the archive and extraction that stops past a byte cap (zip bombs lie about sizes).

import type JSZip from 'jszip';

export interface ZipCaps {
  /** Entries listed; the rest are counted but not shown. */
  maxEntries: number;
  /** Declared uncompressed total above which the archive is treated as a bomb (nothing is extracted). */
  maxTotalBytes: number;
  /** Largest single entry that is extracted for preview. */
  maxEntryBytes: number;
}

export const ZIP_CAPS: ZipCaps = { maxEntries: 5000, maxTotalBytes: 2 * 1024 ** 3, maxEntryBytes: 64 * 1024 ** 2 };

export interface ZipEntryInfo {
  /** Display path: forward slashes, no leading slash. */
  path: string;
  /** The entry's name as stored in the archive (what JSZip looks it up by). */
  zipName: string;
  name: string;
  size: number | null;
  compressed: number | null;
  date: Date | null;
}

export interface ZipListing {
  entries: ZipEntryInfo[];
  /** Every file entry in the archive (folders excluded), even past the cap. */
  count: number;
  totalSize: number;
  truncated: boolean;
  /** Declared sizes past `maxTotalBytes` or absurd compression ratios: listing only. */
  bomb: boolean;
}

interface SizedData {
  uncompressedSize?: number;
  compressedSize?: number;
}

/** Sizes JSZip read from the central directory (internal `_data`, present on loaded archives). */
function sizesOf(file: JSZip.JSZipObject): SizedData {
  const data = (file as unknown as { _data?: SizedData })._data;
  return data && typeof data === 'object' ? data : {};
}

export function listZip(zip: JSZip, caps: ZipCaps = ZIP_CAPS): ZipListing {
  const entries: ZipEntryInfo[] = [];
  let count = 0;
  let total = 0;
  let bomb = false;
  for (const file of Object.values(zip.files)) {
    if (file.dir) continue;
    count++;
    const { uncompressedSize, compressedSize } = sizesOf(file);
    const size = typeof uncompressedSize === 'number' ? uncompressedSize : null;
    const compressed = typeof compressedSize === 'number' ? compressedSize : null;
    if (size !== null) total += size;
    if (size !== null && compressed && size > 10 * 1024 ** 2 && size / compressed > 1000) bomb = true;
    if (entries.length < caps.maxEntries) {
      const path = file.name.replace(/\\/g, '/').replace(/^\/+/, '');
      entries.push({ path, zipName: file.name, name: path.split('/').filter(Boolean).pop() ?? path, size, compressed, date: file.date ?? null });
    }
  }
  if (total > caps.maxTotalBytes) bomb = true;
  entries.sort((a, b) => a.path.localeCompare(b.path));
  return { entries, count, totalSize: total, truncated: count > entries.length, bomb };
}

export interface ZipFolderView {
  folders: { name: string; path: string; count: number; size: number }[];
  files: ZipEntryInfo[];
}

/** Sub-folders and files directly inside `prefix` ('' = archive root). */
export function zipFolder(entries: ZipEntryInfo[], prefix: string): ZipFolderView {
  const head = prefix ? `${prefix.replace(/\/+$/, '')}/` : '';
  const folders = new Map<string, { name: string; path: string; count: number; size: number }>();
  const files: ZipEntryInfo[] = [];
  for (const e of entries) {
    if (!e.path.startsWith(head)) continue;
    const rest = e.path.slice(head.length);
    const slash = rest.indexOf('/');
    if (slash < 0) {
      if (rest) files.push(e);
      continue;
    }
    const name = rest.slice(0, slash);
    const f = folders.get(name) ?? { name, path: head + name, count: 0, size: 0 };
    f.count++;
    f.size += e.size ?? 0;
    folders.set(name, f);
  }
  return { folders: [...folders.values()].sort((a, b) => a.name.localeCompare(b.name)), files };
}

export class ZipCapError extends Error {}

interface ZipStream {
  on(event: 'data', cb: (chunk: Uint8Array) => void): ZipStream;
  on(event: 'error', cb: (err: Error) => void): ZipStream;
  on(event: 'end', cb: () => void): ZipStream;
  pause(): ZipStream;
  resume(): ZipStream;
}

/**
 * One entry's bytes, refusing (and stopping the inflater) once more than `maxBytes` come out,
 * whatever size the archive declared.
 */
export function extractEntry(zip: JSZip, path: string, maxBytes: number): Promise<Uint8Array> {
  const file = zip.file(path);
  if (!file) return Promise.reject(new Error(`${path} is not in the archive`));
  return inflateCapped(file, maxBytes, true).then((out) => out.data!);
}

/** Streams one entry, failing once more than `maxBytes` come out; keeps the bytes only when `keep`. */
function inflateCapped(file: JSZip.JSZipObject, maxBytes: number, keep: boolean): Promise<{ data: Uint8Array | null; total: number }> {
  const declared = sizesOf(file).uncompressedSize;
  if (typeof declared === 'number' && declared > maxBytes) {
    return Promise.reject(new ZipCapError(`This file is too large to preview from the archive (${Math.round(declared / 1024 ** 2)} MB).`));
  }
  return new Promise<{ data: Uint8Array | null; total: number }>((resolve, reject) => {
    const chunks: Uint8Array[] = [];
    let total = 0;
    let done = false;
    // internalStream is public JSZip API (3.x) missing from its typings.
    const stream = (file as unknown as { internalStream: (t: 'uint8array') => ZipStream }).internalStream('uint8array');
    stream
      .on('data', (chunk: Uint8Array) => {
        if (done) return;
        total += chunk.length;
        if (total > maxBytes) {
          done = true;
          stream.pause();
          reject(new ZipCapError('This file inflates past the preview limit (possible zip bomb).'));
          return;
        }
        if (keep) chunks.push(chunk);
      })
      .on('error', (err: Error) => {
        if (done) return;
        done = true;
        reject(err);
      })
      .on('end', () => {
        if (done) return;
        done = true;
        if (!keep) {
          resolve({ data: null, total });
          return;
        }
        const out = new Uint8Array(total);
        let at = 0;
        for (const c of chunks) {
          out.set(c, at);
          at += c.length;
        }
        resolve({ data: out, total });
      })
      .resume();
  });
}

/** One entry as UTF-8 text, refusing past `maxBytes` inflated (the converters' XML parts). */
export async function entryText(zip: JSZip, path: string, maxBytes: number): Promise<string | null> {
  if (!zip.file(path)) return null;
  return new TextDecoder().decode(await extractEntry(zip, path, maxBytes));
}

/**
 * Inflates every entry once, counting, and throws ZipCapError once the archive's real total
 * passes `maxTotal` (declared sizes are not trusted). For converters that unzip internally
 * without a cap (mammoth, SheetJS): checked before handing them the file.
 */
export async function assertInflatesWithin(zip: JSZip, maxTotal: number): Promise<void> {
  let left = maxTotal;
  for (const file of Object.values(zip.files)) {
    if (file.dir) continue;
    try {
      left -= (await inflateCapped(file, left, false)).total;
    } catch (err) {
      if (err instanceof ZipCapError) throw new ZipCapError('This file unpacks to an unusual size (possible zip bomb).');
      throw err;
    }
  }
}
