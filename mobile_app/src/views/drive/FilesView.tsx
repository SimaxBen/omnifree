import { useEffect, useMemo, useRef, useState } from 'react';
import { FileName, FileThumb } from '../../components/files';
import { Icon } from '../../components/icons';
import { Button, Chip, EmptyState, ErrorNote, IconButton, Meter, QuotaBar, SkeletonList, cx, inputClass, riseStyle , Fab } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { useTopic } from '../../lib/bus';
import { breadcrumbs, fileKind, folderOf, folderView, relativeTime, type FileKind } from '../../lib/files';
import { formatBytes } from '../../lib/format';
import { useHubData, useHubEvents } from '../../lib/hub';
import { usePullRefresh } from '../../lib/nav';
import { readPref, writePref } from '../../lib/settings';
import { useUploads } from '../../lib/uploads';
import type { StoredObject } from '../../lib/types';
import { FolderDropSheet } from './FolderDropSheet';

type Filter = 'all' | 'image' | 'doc' | 'media';
const FILTERS: { id: Filter; label: string; kinds: FileKind[] }[] = [
  { id: 'all', label: 'All', kinds: [] },
  { id: 'image', label: 'Photos', kinds: ['image'] },
  { id: 'doc', label: 'Documents', kinds: ['pdf', 'text', 'doc', 'slides', 'sheet', 'code'] },
  { id: 'media', label: 'Audio & video', kinds: ['audio', 'video'] },
];

function UploadJobs() {
  const { jobs, dismiss } = useUploads();
  if (!jobs.length) return null;
  return (
    <div className="space-y-2">
      {jobs.map((j) => (
        <div key={j.id} className="screen-in-up rounded-row bg-ink-900 p-3 ring-1 ring-inset ring-ink-800">
          <div className="flex items-center justify-between gap-2 text-[14px]">
            <span className="flex min-w-0 items-center gap-2">
              <Icon name={j.done ? 'check' : 'upload'} size={16} className={j.done ? 'text-mint-400' : 'text-signal'} />
              <span className="truncate text-ink-100">{j.key}</span>
            </span>
            {j.error ? (
              <button type="button" onClick={() => dismiss(j.id)} className="press min-h-[36px] px-2 text-[13px] text-ink-400 underline">
                Dismiss
              </button>
            ) : (
              <span className="shrink-0 text-ink-400">{j.done ? 'Done' : `${Math.round(j.progress * 100)}%`}</span>
            )}
          </div>
          {j.error ? <p className="pt-1 text-[13px] text-danger-400">{j.error}</p> : <Meter value={j.progress} tone={j.done ? 'ok' : 'off'} className="mt-2 h-1.5 [&>div]:bg-signal" />}
        </div>
      ))}
    </div>
  );
}

export default function FilesView() {
  const actions = useActions();
  const [folder, setFolder] = useState<string>(() => readPref('drive.folder', ''));
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [grid, setGrid] = useState<boolean>(() => readPref('drive.grid', false));
  const [showPool, setShowPool] = useState(false);
  const [sharing, setSharing] = useState<{ id: number; open: boolean } | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query.trim()), 300);
    return () => window.clearTimeout(t);
  }, [query]);
  const crumbsRef = useRef<HTMLElement>(null);
  useEffect(() => {
    writePref('drive.folder', folder);
    // Deep folders overflow the breadcrumb strip: keep the current folder (its end) in view.
    const nav = crumbsRef.current;
    if (nav) nav.scrollLeft = nav.scrollWidth;
  }, [folder]);
  useEffect(() => {
    writePref('drive.grid', grid);
  }, [grid]);

  const capacity = useHubData((c, s) => c.driveCapacity(s), [], 120_000);
  const listing = useHubData((c, s) => c.driveList('', debounced, s), [debounced]);
  useTopic(['drive'], () => {
    listing.reload();
    capacity.reload();
  });
  useHubEvents(['storage.*'], () => {
    listing.reload();
    capacity.reload();
  });
  usePullRefresh(() => {
    listing.reload();
    capacity.reload();
  });

  const view = useMemo(() => {
    const all = listing.data?.objects ?? [];
    const kinds = FILTERS.find((f) => f.id === filter)!.kinds;
    const byKind = (o: StoredObject) => !kinds.length || kinds.includes(fileKind(o.key, o.content_type));
    const sort = (a: StoredObject, b: StoredObject) => (b.last_modified ?? '').localeCompare(a.last_modified ?? '') || a.key.localeCompare(b.key);
    // Searching or filtering shows matches from every folder; browsing shows one folder.
    if (debounced || filter !== 'all') return { folders: [], files: all.filter(byKind).sort(sort), flat: true };
    const v = folderView(all, folder);
    return { folders: v.folders, files: v.files.sort(sort), flat: false };
  }, [listing.data, folder, debounced, filter]);

  const backends = Object.keys(capacity.data ?? {}).sort();
  const used = backends.reduce((s, b) => s + (capacity.data?.[b].used ?? 0), 0);
  const limit = backends.reduce((s, b) => s + (capacity.data?.[b].limit ?? 0), 0);
  const errors = Object.entries(listing.data?.errors ?? {});
  const crumbs = breadcrumbs(folder);
  const inFolder = useMemo(() => {
    const prefix = `${folder}/`;
    const objs = folder ? (listing.data?.objects ?? []).filter((o) => o.key.startsWith(prefix)) : [];
    return { count: objs.length, size: objs.reduce((s, o) => s + (o.size ?? 0), 0) };
  }, [listing.data, folder]);

  return (
    <div className="space-y-4">
      <button type="button" onClick={() => setShowPool((v) => !v)} className="press block w-full rounded-card bg-ink-900 p-4 text-left ring-1 ring-inset ring-ink-800" aria-expanded={showPool}>
        <span className="flex items-baseline justify-between gap-2">
          <span className="font-display text-[22px] font-semibold text-ink-100">
            {formatBytes(used)} <span className="font-sans text-[14px] font-normal text-ink-400">of {limit ? formatBytes(limit) : 'unknown'} free storage</span>
          </span>
          <Icon name={showPool ? 'up' : 'down'} size={16} className="text-ink-500" />
        </span>
        <Meter value={limit ? used / limit : null} className="mt-2" />
        {showPool && (
          <span className="screen-in-up mt-4 block space-y-3">
            {backends.map((name) => (
              <QuotaBar key={name} quota={capacity.data![name]} label={name} />
            ))}
            {capacity.error && <span className="block text-[13px] text-danger-400">{capacity.error}</span>}
          </span>
        )}
      </button>

      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <Icon name="search" size={18} className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-500" />
          <input type="search" aria-label="Search files" className={cx(inputClass, 'pl-11')} placeholder="Search every backend" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <IconButton icon={grid ? 'table' : 'grid'} label={grid ? 'Show as list' : 'Show as grid'} onClick={() => setGrid((v) => !v)} className="h-[46px] w-[46px] bg-ink-850" />
      </div>

      <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4">
        {FILTERS.map((f) => (
          <Chip key={f.id} active={filter === f.id} onClick={() => setFilter(f.id)}>
            {f.label}
          </Chip>
        ))}
      </div>

      <UploadJobs />

      {!view.flat && (
        <div className="-mx-4 flex items-center gap-2 pr-4">
          <nav ref={crumbsRef} aria-label="Folder" className="no-scrollbar flex min-w-0 flex-1 items-center gap-1 overflow-x-auto pl-4">
            {crumbs.map((c, i) => (
              <span key={c.path} className="flex shrink-0 items-center gap-1">
                {i > 0 && <Icon name="right" size={14} className="text-ink-600" />}
                <button
                  type="button"
                  onClick={() => setFolder(c.path)}
                  aria-current={i === crumbs.length - 1 ? 'location' : undefined}
                  className={cx('press min-h-[40px] rounded-full px-2.5 text-[14px]', i === crumbs.length - 1 ? 'font-semibold text-ink-100' : 'text-ink-400')}
                >
                  {c.name}
                </button>
              </span>
            ))}
          </nav>
          {/* Outside the scrolling strip, so it never ends up past the right edge in deep folders. */}
          {folder && inFolder.count > 0 && (
            <button
              type="button"
              onClick={() => setSharing((s) => ({ id: (s?.id ?? 0) + 1, open: true }))}
              className="press flex min-h-[40px] shrink-0 items-center gap-1.5 rounded-full bg-ink-850 px-3 text-[13px] text-ink-200 ring-1 ring-inset ring-ink-700"
            >
              <Icon name="share" size={14} />
              Share folder
            </button>
          )}
        </div>
      )}
      {sharing && (
        <FolderDropSheet
          key={sharing.id}
          open={sharing.open}
          folder={folder}
          files={inFolder.count}
          size={inFolder.size}
          onClose={() => setSharing((s) => (s ? { ...s, open: false } : s))}
        />
      )}

      {listing.error && <ErrorNote message={listing.error} onRetry={listing.reload} />}
      {errors.length > 0 && <p className="px-1 text-[13px] text-amber-400">Unreachable: {errors.map(([k, v]) => `${k} (${v})`).join(', ')}</p>}
      {listing.loading && !listing.data && <SkeletonList rows={6} />}

      {view.folders.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {view.folders.map((f, i) => (
            <button
              key={f.path}
              type="button"
              onClick={() => setFolder(f.path)}
              style={riseStyle(i)}
              className="press rise flex min-h-[64px] items-center gap-3 rounded-row bg-ink-900 px-3 text-left ring-1 ring-inset ring-ink-800"
            >
              <Icon name="folder" size={24} className="shrink-0 text-signal" />
              <span className="min-w-0">
                <span className="block truncate text-[15px] text-ink-100">{f.name}</span>
                <span className="block text-[12px] text-ink-500">
                  {f.count} file{f.count === 1 ? '' : 's'}, {formatBytes(f.size)}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}

      {listing.data && !view.files.length && !view.folders.length && (
        <EmptyState
          icon="drive"
          title={debounced ? 'No files match' : filter !== 'all' ? 'Nothing of that type' : folder ? 'This folder is empty' : 'Your Drive is empty'}
          hint={debounced ? 'Search looks at file names and metadata on every backend.' : 'Uploads go to the backend with the most free space.'}
          action={
            !debounced && (
              <Button variant="primary" icon="upload" onClick={() => actions.pickUpload(folder)}>
                Upload here
              </Button>
            )
          }
        />
      )}

      {view.files.length > 0 &&
        (grid ? (
          <div className="grid grid-cols-3 gap-2">
            {view.files.map((o, i) => (
              <button
                key={`${o.backend}/${o.key}`}
                type="button"
                onClick={() => actions.preview(o, view.files)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  actions.fileActions(o);
                }}
                style={riseStyle(i)}
                className="press rise text-left"
              >
                <FileThumb obj={o} fill className="rounded-[16px]" />
                <span className="mt-1 block truncate text-[13px] text-ink-200">{o.key.split('/').pop()}</span>
              </button>
            ))}
          </div>
        ) : (
          <ul className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {view.files.map((o, i) => (
              <li key={`${o.backend}/${o.key}`} className="rise flex items-center gap-1 pl-3" style={riseStyle(i)}>
                <button type="button" onClick={() => actions.preview(o, view.files)} className="flex min-h-[64px] min-w-0 flex-1 items-center gap-3 py-2 text-left">
                  <FileThumb obj={o} size={44} />
                  <span className="min-w-0 flex-1">
                    <FileName objKey={o.key} />
                    <span className="block truncate text-[12px] text-ink-500">
                      {formatBytes(o.size)}
                      {o.last_modified ? `, ${relativeTime(o.last_modified)}` : ''}
                      {view.flat && folderOf(o.key) ? `, in ${folderOf(o.key)}` : ''}
                    </span>
                  </span>
                </button>
                <IconButton icon="more" label={`Actions for ${o.key}`} onClick={() => actions.fileActions(o)} />
              </li>
            ))}
          </ul>
        ))}

      <Fab icon="upload" label={`Upload to ${folder || 'Drive'}`} onClick={() => actions.pickUpload(view.flat ? '' : folder)} />
    </div>
  );
}
