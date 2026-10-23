// Zip archives: folders and files with sizes and dates; tapping a file opens it in the same
// previewer (nested, with back), with "Download this file". Capped against zip bombs: entries
// listed, declared total size, and bytes actually inflated per entry.

import JSZip from 'jszip';
import { useEffect, useMemo, useState } from 'react';
import { fileKind } from '../../lib/files';
import { formatBytes } from '../../lib/format';
import { errorText } from '../../lib/hub';
import { useOverlayBack } from '../../lib/overlay';
import { ZIP_CAPS, extractEntry, listZip, zipFolder, type ZipEntryInfo, type ZipListing } from '../../lib/preview/zip';
import { memorySource, mimeOf, type PreviewSource } from '../../lib/preview/source';
import { FileTile } from '../fileTile';
import { Icon } from '../icons';
import { IconButton, Spinner } from '../ui';
import { Loading, PreviewError, useSourceBytes } from './common';
import { PreviewBody } from './PreviewBody';

export const ZIP_MAX_BYTES = 100 * 1024 * 1024;

/** A file opened from the archive. The Android back gesture closes it (only it: overlays stack). */
function NestedViewer({ entry, src, depth, onBack }: { entry: ZipEntryInfo; src: PreviewSource; depth: number; onBack: () => void }) {
  const back = useOverlayBack(true, onBack);
  return (
    <div className="absolute inset-0 z-10 flex flex-col bg-[#05060f]">
      <div className="flex shrink-0 items-center gap-1 border-b border-ink-800 bg-ink-900 px-1 py-1">
        <IconButton icon="left" label="Back to the archive" onClick={() => back()} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[14px] text-ink-100">{entry.name}</p>
          <p className="truncate text-[11px] text-ink-500">
            {entry.path}
            {entry.size !== null ? `, ${formatBytes(entry.size)}` : ''}
          </p>
        </div>
        <IconButton icon="download" label="Download this file" onClick={() => void src.download()} />
      </div>
      <div className="min-h-0 flex-1">
        <PreviewBody src={src} depth={depth + 1} />
      </div>
    </div>
  );
}

export default function ZipPreview({ src, depth = 0 }: { src: PreviewSource; depth?: number }) {
  const { data, error: loadError } = useSourceBytes(src, ZIP_MAX_BYTES);
  const [zip, setZip] = useState<{ zip: JSZip; listing: ZipListing } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [folder, setFolder] = useState('');
  const [opening, setOpening] = useState<string | null>(null);
  const [opened, setOpened] = useState<{ entry: ZipEntryInfo; src: PreviewSource & { dispose: () => void } } | null>(null);
  const [entryError, setEntryError] = useState<string | null>(null);

  useEffect(() => {
    if (!data) return undefined;
    let alive = true;
    setZip(null);
    setError(null);
    JSZip.loadAsync(data).then(
      (z) => alive && setZip({ zip: z, listing: listZip(z) }),
      (err) => alive && setError(/encrypt/i.test(String(err)) ? 'This archive is encrypted (password-protected).' : /zip|central directory|end of data/i.test(String(err)) ? 'This file is damaged or not a zip archive.' : err),
    );
    return () => {
      alive = false;
    };
  }, [data]);

  useEffect(() => () => opened?.src.dispose(), [opened]);

  const view = useMemo(() => (zip ? zipFolder(zip.listing.entries, folder) : null), [zip, folder]);

  if (loadError || error) return <PreviewError src={src} error={loadError || error} />;
  if (!zip || !view) return <Loading label={data ? 'Reading the archive' : 'Downloading the archive'} />;
  const { listing } = zip;

  const open = async (entry: ZipEntryInfo) => {
    if (listing.bomb) return;
    setEntryError(null);
    setOpening(entry.path);
    try {
      const bytes = await extractEntry(zip.zip, entry.zipName, ZIP_CAPS.maxEntryBytes);
      setOpened({ entry, src: memorySource(`${src.id}!${entry.path}`, entry.path, bytes, mimeOf(entry.path)) });
    } catch (err) {
      setEntryError(`${entry.name}: ${errorText(err)}`);
    } finally {
      setOpening(null);
    }
  };

  const crumbs = folder ? folder.split('/') : [];
  return (
    <div className="relative flex h-full flex-col bg-ink-950">
      <div className="shrink-0 space-y-1 border-b border-ink-800 bg-ink-900 px-3 py-2 text-[12px] text-ink-400">
        <p>
          {listing.count.toLocaleString()} file{listing.count === 1 ? '' : 's'}, {formatBytes(listing.totalSize)} unpacked
          {listing.truncated ? `, first ${listing.entries.length.toLocaleString()} listed` : ''}
        </p>
        {listing.bomb && <p className="text-amber-400">This archive unpacks to an unusual size (possible zip bomb): files are listed but not opened.</p>}
        {entryError && <p className="text-danger-400">{entryError}</p>}
      </div>
      <nav aria-label="Folder in the archive" className="no-scrollbar flex shrink-0 items-center gap-1 overflow-x-auto px-2 py-1">
        {folder && <IconButton icon="up" label="Parent folder" onClick={() => setFolder(crumbs.slice(0, -1).join('/'))} className="h-10 w-10" />}
        <button type="button" onClick={() => setFolder('')} className="press min-h-[40px] shrink-0 rounded-full px-2 text-[14px] text-ink-300">
          {src.name}
        </button>
        {crumbs.map((c, i) => (
          <span key={i} className="flex shrink-0 items-center gap-1">
            <Icon name="right" size={14} className="text-ink-600" />
            <button type="button" onClick={() => setFolder(crumbs.slice(0, i + 1).join('/'))} className="press min-h-[40px] rounded-full px-2 text-[14px] text-ink-100">
              {c}
            </button>
          </span>
        ))}
      </nav>
      <ul className="min-h-0 flex-1 divide-y divide-ink-800 overflow-y-auto overscroll-contain">
        {view.folders.map((f) => (
          <li key={f.path}>
            <button type="button" onClick={() => setFolder(f.path)} className="press flex min-h-[60px] w-full items-center gap-3 px-3 text-left">
              <Icon name="folder" size={26} className="shrink-0 text-signal" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{f.name}</span>
                <span className="block text-[12px] text-ink-500">
                  {f.count} file{f.count === 1 ? '' : 's'}, {formatBytes(f.size)}
                </span>
              </span>
              <Icon name="right" size={16} className="text-ink-600" />
            </button>
          </li>
        ))}
        {view.files.map((e) => (
          <li key={e.path}>
            <button type="button" onClick={() => void open(e)} disabled={listing.bomb || !!opening} className="press flex min-h-[60px] w-full items-center gap-3 px-3 text-left disabled:opacity-60">
              <FileTile kind={fileKind(e.name)} size={38} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-ink-100">{e.name}</span>
                <span className="block text-[12px] text-ink-500">
                  {e.size !== null ? formatBytes(e.size) : 'size unknown'}
                  {e.date ? `, ${e.date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}` : ''}
                </span>
              </span>
              {opening === e.path && <Spinner />}
            </button>
          </li>
        ))}
        {!view.folders.length && !view.files.length && <li className="p-8 text-center text-[14px] text-ink-400">This archive is empty.</li>}
      </ul>
      {opened && <NestedViewer entry={opened.entry} src={opened.src} depth={depth} onBack={() => setOpened(null)} />}
    </div>
  );
}
