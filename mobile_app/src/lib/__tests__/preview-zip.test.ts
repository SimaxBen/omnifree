import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';
import { ZipCapError, assertInflatesWithin, entryText, extractEntry, listZip, zipFolder, type ZipCaps } from '../preview/zip';
import { ZipFormatError, findCentralDirectory, localDataStart, openRemoteZip, parseCentralDirectory } from '../thumbs/zipRange';

async function roundTrip(z: JSZip): Promise<JSZip> {
  return JSZip.loadAsync(await z.generateAsync({ type: 'uint8array', compression: 'DEFLATE' }));
}

const CAPS: ZipCaps = { maxEntries: 5, maxTotalBytes: 10_000, maxEntryBytes: 4_000 };

describe('zip listing', () => {
  it('lists files with sizes and dates, folders excluded', async () => {
    const z = new JSZip();
    z.file('docs/a.txt', 'hello', { date: new Date('2026-01-02T03:04:05Z') });
    z.file('docs/sub/b.md', '# b');
    z.file('c.pdf', 'pdf');
    z.folder('empty');
    const l = listZip(await roundTrip(z), CAPS);
    expect(l.count).toBe(3);
    expect(l.truncated).toBe(false);
    expect(l.bomb).toBe(false);
    expect(l.totalSize).toBe(5 + 3 + 3);
    expect(l.entries.map((e) => [e.path, e.name, e.size])).toEqual([
      ['c.pdf', 'c.pdf', 3],
      ['docs/a.txt', 'a.txt', 5],
      ['docs/sub/b.md', 'b.md', 3],
    ]);
    expect(l.entries[1].date).toBeInstanceOf(Date);

    const root = zipFolder(l.entries, '');
    expect(root.folders).toEqual([{ name: 'docs', path: 'docs', count: 2, size: 8 }]);
    expect(root.files.map((f) => f.name)).toEqual(['c.pdf']);
    const docs = zipFolder(l.entries, 'docs');
    expect(docs.folders.map((f) => f.path)).toEqual(['docs/sub']);
    expect(docs.files.map((f) => f.name)).toEqual(['a.txt']);
  });

  it('caps the number of entries listed', async () => {
    const z = new JSZip();
    for (let i = 0; i < 12; i++) z.file(`f${String(i).padStart(2, '0')}.txt`, 'x');
    const l = listZip(await roundTrip(z), CAPS);
    expect(l.count).toBe(12);
    expect(l.entries).toHaveLength(5);
    expect(l.truncated).toBe(true);
  });

  it('flags archives whose declared size passes the cap (zip bombs)', async () => {
    const z = new JSZip();
    z.file('zeros.bin', new Uint8Array(20_000)); // deflates to a few bytes
    const l = listZip(await roundTrip(z), CAPS);
    expect(l.totalSize).toBe(20_000);
    expect(l.bomb).toBe(true);
    expect(listZip(await roundTrip(z), { ...CAPS, maxTotalBytes: 50_000 }).bomb).toBe(false);
  });

  it('extracts an entry, refusing declared or actual sizes past the cap', async () => {
    const z = new JSZip();
    z.file('ok.txt', 'fine');
    z.file('big.bin', new Uint8Array(5_000));
    z.file('liar.bin', new Uint8Array(9_000).fill(7));
    const zip = await roundTrip(z);
    expect(new TextDecoder().decode(await extractEntry(zip, 'ok.txt', 4_000))).toBe('fine');
    await expect(extractEntry(zip, 'big.bin', 4_000)).rejects.toBeInstanceOf(ZipCapError);
    // A header that understates the size: the inflater is stopped once the cap is passed.
    (zip.file('liar.bin') as unknown as { _data: { uncompressedSize: number } })._data.uncompressedSize = 10;
    await expect(extractEntry(zip, 'liar.bin', 4_000)).rejects.toBeInstanceOf(ZipCapError);
    await expect(extractEntry(zip, 'missing', 4_000)).rejects.toThrow(/not in the archive/);
  });
  it('opens entries stored with Windows separators by their stored name', async () => {
    const z = new JSZip();
    const stored = ['photos', 'trip', 'notes.txt'].join(String.fromCharCode(92)); // backslashes
    z.file(stored, 'from windows');
    const zip = await roundTrip(z);
    const [e] = listZip(zip, CAPS).entries;
    expect(e.path).toBe('photos/trip/notes.txt');
    expect(e.zipName).toBe(stored);
    expect(new TextDecoder().decode(await extractEntry(zip, e.zipName, 4_000))).toBe('from windows');
  });

  it('checks the real unpacked total before uncapped converters run (lying headers included)', async () => {
    const z = new JSZip();
    z.file('word/document.xml', '<w:t>a</w:t>');
    z.file('word/media/bomb.bin', new Uint8Array(50_000));
    const zip = await roundTrip(z);
    await expect(assertInflatesWithin(zip, 100_000)).resolves.toBeUndefined();
    await expect(assertInflatesWithin(zip, 20_000)).rejects.toBeInstanceOf(ZipCapError);
    (zip.file('word/media/bomb.bin') as unknown as { _data: { uncompressedSize: number } })._data.uncompressedSize = 1;
    await expect(assertInflatesWithin(zip, 20_000)).rejects.toBeInstanceOf(ZipCapError);
  });

  it('reads XML parts as text under a cap', async () => {
    const z = new JSZip();
    z.file('content.xml', 'x'.repeat(5_000));
    const zip = await roundTrip(z);
    expect(await entryText(zip, 'content.xml', 10_000)).toHaveLength(5_000);
    expect(await entryText(zip, 'missing.xml', 10_000)).toBeNull();
    await expect(entryText(zip, 'content.xml', 1_000)).rejects.toBeInstanceOf(ZipCapError);
  });
});

describe('zip by range', () => {
  it('reads the central directory and one entry from byte ranges', async () => {
    const z = new JSZip();
    z.file('docProps/thumbnail.jpeg', new Uint8Array([0xff, 0xd8, 1, 2, 3]), { compression: 'STORE' });
    z.file('word/document.xml', '<w:t>Hello</w:t>'.repeat(200));
    const bytes = await z.generateAsync({ type: 'uint8array', compression: 'DEFLATE', comment: 'a comment' });
    const tail = bytes.subarray(Math.max(0, bytes.length - 200));
    const cd = findCentralDirectory(tail, bytes.length);
    const entries = parseCentralDirectory(bytes.subarray(cd.offset, cd.offset + cd.size));
    expect(entries.map((e) => e.name)).toEqual(['docProps/', 'docProps/thumbnail.jpeg', 'word/', 'word/document.xml']);
    expect(entries.length).toBe(cd.count);
    const thumb = entries[1];
    expect(thumb.method).toBe(0);
    expect(localDataStart(bytes.subarray(thumb.localOffset))).toBe(30 + 'docProps/thumbnail.jpeg'.length);

    const asked: [number, number][] = [];
    const remote = await openRemoteZip(async (s, e) => {
      asked.push([s, e]);
      return bytes.slice(s, e + 1).buffer;
    }, bytes.length);
    expect(Array.from((await remote.read('docProps/thumbnail.jpeg', 1000))!)).toEqual([0xff, 0xd8, 1, 2, 3]);
    expect(asked.every(([s, e]) => e < bytes.length && s >= 0)).toBe(true);
    expect(await remote.read('nope', 1000)).toBeNull();
    const full = new TextDecoder().decode((await remote.read('word/document.xml', 10_000))!);
    expect(full).toBe('<w:t>Hello</w:t>'.repeat(200));
    expect(await remote.read('word/document.xml', 100)).toBeNull(); // too large unless partial
    const head = new TextDecoder().decode((await remote.read('word/document.xml', 40, { maxCompressed: 1_000 }))!);
    expect(head).toBe('<w:t>Hello</w:t>'.repeat(3).slice(0, 40));
  });

  it('ignores an end-of-directory signature planted in the archive comment', async () => {
    const z = new JSZip();
    z.file('docProps/thumbnail.jpeg', new Uint8Array([0xff, 0xd8, 9]), { compression: 'STORE' });
    // A fake EOCD (count 1, size 10, offset 0, comment length 0) inside the comment text.
    const fake = String.fromCharCode(0x50, 0x4b, 0x05, 0x06, 0, 0, 0, 0, 1, 0, 1, 0, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0);
    const bytes = await z.generateAsync({ type: 'uint8array', comment: `note ${fake} end` });
    const cd = findCentralDirectory(bytes, bytes.length);
    expect(cd.count).toBe(2); // docProps/ and the picture, not the fake record's 1
    const remote = await openRemoteZip(async (s, e) => bytes.slice(s, e + 1).buffer, bytes.length);
    expect(Array.from((await remote.read('docProps/thumbnail.jpeg', 100))!)).toEqual([0xff, 0xd8, 9]);
  });

  it('refuses ZIP64 end records instead of misreading them', () => {
    const eocd = new Uint8Array(22);
    const v = new DataView(eocd.buffer);
    v.setUint32(0, 0x06054b50, true);
    v.setUint16(10, 0xffff, true);
    v.setUint32(12, 0xffffffff, true);
    v.setUint32(16, 0xffffffff, true);
    expect(() => findCentralDirectory(eocd, 5_000_000_000)).toThrow(ZipFormatError);
  });
});
