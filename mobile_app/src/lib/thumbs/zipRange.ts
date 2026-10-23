// Reads single entries of a remote zip (docx/xlsx/pptx/odt are zips) with HTTP Range requests:
// the end of the file (central directory), then just the wanted entry. A 10 MB .docx costs a
// few dozen KB to find its embedded thumbnail. No ZIP64 (Office files are far below 4 GB).

export interface CdEntry {
  name: string;
  method: number;
  flags: number;
  compressedSize: number;
  size: number;
  localOffset: number;
}

const EOCD_SIG = 0x06054b50;
const CD_SIG = 0x02014b50;
const LOCAL_SIG = 0x04034b50;
/** End-of-central-directory record (22) plus the longest comment (65535). */
export const TAIL_BYTES = 22 + 0xffff;

export class ZipFormatError extends Error {}

/**
 * Locates the central directory from the last bytes of a zip of `fileSize` bytes. The archive
 * comment (up to 64 KB) may itself contain the EOCD signature: a record whose comment length
 * reaches exactly the end of the file wins; a plausible one is the fallback (trailing bytes).
 */
export function findCentralDirectory(tail: Uint8Array, fileSize: number): { offset: number; size: number; count: number } {
  const v = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
  let fallback: { offset: number; size: number; count: number } | null = null;
  for (let i = tail.length - 22; i >= 0; i--) {
    if (v.getUint32(i, true) !== EOCD_SIG) continue;
    const count = v.getUint16(i + 10, true);
    const size = v.getUint32(i + 12, true);
    const offset = v.getUint32(i + 16, true);
    const commentLen = v.getUint16(i + 20, true);
    const exact = i + 22 + commentLen === tail.length;
    if (offset === 0xffffffff || size === 0xffffffff || count === 0xffff) {
      if (exact) throw new ZipFormatError('ZIP64 archives are not read by range');
      continue;
    }
    if (offset + size > fileSize) continue; // a stray signature inside the comment
    if (exact) return { offset, size, count };
    fallback ??= { offset, size, count };
  }
  if (fallback) return fallback;
  throw new ZipFormatError('Not a zip file');
}

export function parseCentralDirectory(cd: Uint8Array): CdEntry[] {
  const v = new DataView(cd.buffer, cd.byteOffset, cd.byteLength);
  const dec = new TextDecoder();
  const out: CdEntry[] = [];
  let p = 0;
  while (p + 46 <= cd.length && v.getUint32(p, true) === CD_SIG) {
    const flags = v.getUint16(p + 8, true);
    const method = v.getUint16(p + 10, true);
    const compressedSize = v.getUint32(p + 20, true);
    const size = v.getUint32(p + 24, true);
    const nameLen = v.getUint16(p + 28, true);
    const extraLen = v.getUint16(p + 30, true);
    const commentLen = v.getUint16(p + 32, true);
    const localOffset = v.getUint32(p + 42, true);
    const name = dec.decode(cd.subarray(p + 46, p + 46 + nameLen));
    out.push({ name, method, flags, compressedSize, size, localOffset });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

/** Offset of an entry's data, given the bytes starting at its local header. */
export function localDataStart(head: Uint8Array): number {
  const v = new DataView(head.buffer, head.byteOffset, head.byteLength);
  if (head.length < 30 || v.getUint32(0, true) !== LOCAL_SIG) throw new ZipFormatError('Bad local header');
  return 30 + v.getUint16(26, true) + v.getUint16(28, true);
}

/**
 * Inflates raw deflate data (zip method 8). Past `maxOut` bytes it stops: with an error, or with
 * the first `maxOut` bytes when `partial` (enough for "the first words of a document").
 */
export async function inflateRaw(data: Uint8Array, maxOut: number, partial = false): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    if (total + value.length > maxOut) {
      await reader.cancel().catch(() => undefined);
      if (!partial) throw new ZipFormatError('Entry inflates past its limit');
      chunks.push(value.subarray(0, maxOut - total));
      total = maxOut;
      break;
    }
    total += value.length;
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

/** Inflates the start of a deflate stream cut short: whatever came out before the input ran out. */
async function inflatePrefix(data: Uint8Array, maxOut: number): Promise<Uint8Array> {
  const reader = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (total < maxOut) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value.subarray(0, maxOut - total));
      total += Math.min(value.length, maxOut - total);
    }
    await reader.cancel().catch(() => undefined);
  } catch {
    // "unexpected end of data": expected, the input was cut.
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

export type RangeFetcher =(start: number, end: number) => Promise<ArrayBuffer>;

export interface RemoteZip {
  entries: CdEntry[];
  /**
   * An entry's bytes (stored or deflated), or null when absent, encrypted or too large. With
   * `partial`, a large entry gives its first `maxOut` bytes instead (only `maxCompressed` are fetched).
   */
  read(name: string, maxOut: number, partial?: { maxCompressed: number }): Promise<Uint8Array | null>;
}

export async function openRemoteZip(fetchRange: RangeFetcher, fileSize: number, maxDirectory = 4 * 1024 * 1024): Promise<RemoteZip> {
  const tailStart = Math.max(0, fileSize - TAIL_BYTES);
  const tail = new Uint8Array(await fetchRange(tailStart, fileSize - 1));
  const cd = findCentralDirectory(tail, fileSize);
  if (cd.size > maxDirectory) throw new ZipFormatError('Central directory too large');
  const cdBytes =
    cd.offset >= tailStart
      ? tail.subarray(cd.offset - tailStart, cd.offset - tailStart + cd.size)
      : new Uint8Array(await fetchRange(cd.offset, cd.offset + cd.size - 1));
  const entries = parseCentralDirectory(cdBytes);
  return {
    entries,
    async read(name, maxOut, partial) {
      const e = entries.find((x) => x.name === name);
      if (!e || e.flags & 1) return null;
      if (!partial && (e.size > maxOut || e.compressedSize > maxOut)) return null;
      const want = partial ? Math.min(e.compressedSize, partial.maxCompressed) : e.compressedSize;
      // Local name/extra lengths may differ from the central copy: fetch some slack, then slice.
      const head = new Uint8Array(await fetchRange(e.localOffset, Math.min(fileSize - 1, e.localOffset + 30 + 1024 + want)));
      const start = localDataStart(head);
      let raw = head.subarray(start, start + want);
      if (raw.length < want) {
        raw = new Uint8Array(await fetchRange(e.localOffset + start, e.localOffset + start + want - 1));
      }
      if (e.method === 0) return raw.slice(0, maxOut);
      if (e.method === 8) {
        // A cut deflate stream ends in an error once its bytes run out: keep what came before.
        if (want < e.compressedSize) return inflatePrefix(raw, maxOut);
        return inflateRaw(raw, maxOut, !!partial);
      }
      return null;
    },
  };
}
