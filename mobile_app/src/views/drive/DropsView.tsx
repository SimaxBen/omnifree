import { useMemo, useRef, useState } from 'react';
import { FileName, FileThumb, FileTile } from '../../components/files';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { SwipeRow } from '../../components/SwipeRow';
import { Badge, Button, EmptyState, ErrorNote, SkeletonList, cx, inputClass, riseStyle } from '../../components/ui';
import { useActions } from '../../lib/actions';
import { announce, useTopic } from '../../lib/bus';
import { fileKind, relativeTime } from '../../lib/files';
import { formatBytes, formatDateTime } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { usePullRefresh } from '../../lib/nav';
import type { Drop, FolderDrop, StoredObject } from '../../lib/types';
import { DropResult } from './DriveSheets';

function expired(d: Drop): boolean {
  return new Date(d.expires_at).getTime() < Date.now() || (d.max_downloads !== null && d.downloads >= d.max_downloads);
}

function PickFileSheet({ open, onClose, onPick }: { open: boolean; onClose: () => void; onPick: (o: StoredObject) => void }) {
  const files = useHubData((c, s) => c.driveList('', '', s), []);
  const [q, setQ] = useState('');
  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (files.data?.objects ?? [])
      .filter((o) => !needle || o.key.toLowerCase().includes(needle))
      .sort((a, b) => (b.last_modified ?? '').localeCompare(a.last_modified ?? ''));
  }, [files.data, q]);
  return (
    <Sheet open={open} onClose={onClose} full title="Choose a file to share">
      <input type="search" aria-label="Filter files" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by name" className={cx(inputClass, 'mb-3')} />
      {files.loading && !files.data && <SkeletonList rows={6} />}
      {files.error && <ErrorNote message={files.error} onRetry={files.reload} />}
      <ul className="-mx-5 divide-y divide-ink-800">
        {list.map((o) => (
          <li key={`${o.backend}/${o.key}`}>
            <button type="button" onClick={() => onPick(o)} className="flex min-h-[60px] w-full items-center gap-3 px-5 py-2 text-left active:bg-ink-850">
              <FileThumb obj={o} size={40} />
              <span className="min-w-0 flex-1">
                <FileName objKey={o.key} />
                <span className="block text-[12px] text-ink-500">
                  {formatBytes(o.size)}
                  {o.last_modified ? `, ${relativeTime(o.last_modified)}` : ''}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Sheet>
  );
}

export default function DropsView() {
  const { client, notify } = useHub();
  const actions = useActions();
  const drops = useHubData((c, s) => c.drops(s), []);
  const [picking, setPicking] = useState<{ id: number; open: boolean } | null>(null);
  const [showing, setShowing] = useState<{ id: number; open: boolean; drop: Drop } | null>(null);
  const ids = useRef(1);
  useTopic(['drops'], drops.reload);
  usePullRefresh(drops.reload);

  const remove = async (d: Drop) => {
    try {
      await client.deleteDrop(d.code);
      drops.setData((ds) => ds?.filter((x) => x.code !== d.code));
      announce('drops');
      notify('Drop link removed', 'ok');
      return true;
    } catch (err) {
      notify(errorText(err), 'error');
      return false;
    }
  };

  const list = drops.data ?? [];
  return (
    <div className="space-y-4">
      <Button variant="primary" icon="share" className="w-full" onClick={() => setPicking({ id: ids.current++, open: true })}>
        Share a file
      </Button>
      {drops.error && <ErrorNote message={drops.error} onRetry={drops.reload} />}
      {drops.loading && !drops.data && <SkeletonList rows={3} />}
      {drops.data && !list.length && <EmptyState icon="share" title="No drops" hint="A drop is a download link for one Drive file, or a whole folder (Share folder in the Drive), that expires on its own, with an optional password." />}
      <div className="space-y-2">
        {list.map((d, i) => {
          const dead = expired(d);
          const isFolder = (d as FolderDrop).kind === 'folder';
          return (
            <div key={d.code} className="rise" style={riseStyle(i)}>
              <SwipeRow end={{ icon: 'trash', label: 'Remove', tone: 'danger', run: () => remove(d) }}>
                <button type="button" onClick={() => setShowing({ id: ids.current++, open: true, drop: d })} className="flex min-h-[68px] w-full items-center gap-3 px-3 py-2 text-left">
                  {isFolder ? (
                    <span className={cx('grid h-11 w-11 shrink-0 place-items-center rounded-[14px] bg-signal/15 text-signal', dead && 'opacity-40')}>
                      <Icon name="folder" size={24} />
                    </span>
                  ) : (
                    <FileTile kind={fileKind(d.filename)} size={44} className={dead ? 'opacity-40' : ''} />
                  )}
                  <span className="min-w-0 flex-1">
                    <FileName objKey={d.filename} />
                    <span className="flex flex-wrap items-center gap-x-2 text-[12px] text-ink-500">
                      {isFolder && <span>Folder, {(d as FolderDrop).file_count ?? '?'} files</span>}
                      <span>{dead ? 'Expired' : `Until ${formatDateTime(d.expires_at)}`}</span>
                      <span>
                        {d.downloads}
                        {d.max_downloads !== null ? ` of ${d.max_downloads}` : ''} {isFolder ? 'openings' : 'downloads'}
                      </span>
                    </span>
                  </span>
                  {d.has_password && <Icon name="lock" size={16} className="text-signal" />}
                  {dead && <Badge tone="bad">ended</Badge>}
                </button>
              </SwipeRow>
            </div>
          );
        })}
      </div>
      {list.length > 0 && <p className="text-center text-[12px] text-ink-500">Swipe left to remove a drop.</p>}

      {picking && (
        <PickFileSheet
          key={picking.id}
          open={picking.open}
          onClose={() => setPicking((p) => (p ? { ...p, open: false } : p))}
          onPick={(o) => {
            setPicking((p) => (p ? { ...p, open: false } : p));
            actions.shareDrop(o);
          }}
        />
      )}
      {showing && (
        <Sheet key={showing.id} open={showing.open} onClose={() => setShowing((s) => (s ? { ...s, open: false } : s))} title={showing.drop.filename}>
          <DropResult drop={showing.drop} />
        </Sheet>
      )}
    </div>
  );
}
