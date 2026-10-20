import { useMemo, useState } from 'react';
import { FileThumb, FileName, FileTile, objectUrl } from '../../components/files';
import { Icon } from '../../components/icons';
import { QrCode } from '../../components/QrCode';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, ErrorNote, Field, ListRow, Toggle, cx, haptic, inputClass } from '../../components/ui';
import { DIRECT_UPLOAD_THRESHOLD } from '../../lib/api';
import { announce } from '../../lib/bus';
import { copyText } from '../../lib/dom';
import { allFolders, fileKind, folderOf, isIndexable, joinKey, sanitizeFileName, sanitizeFolder } from '../../lib/files';
import { formatBytes, formatDateTime } from '../../lib/format';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { useUploads } from '../../lib/uploads';
import type { Drop, StoredObject } from '../../lib/types';

// ------------------------------------------------------------------ upload: rename + folder

interface Pending {
  file: File;
  name: string;
}

export function UploadSheet({ open, files, folder: initialFolder, onClose }: { open: boolean; files: File[]; folder: string; onClose: () => void }) {
  const { upload } = useUploads();
  const listing = useHubData((c, s) => c.driveList('', '', s), []);
  const capacity = useHubData((c, s) => c.driveCapacity(s), []);
  const [items, setItems] = useState<Pending[]>(() => files.map((f) => ({ file: f, name: sanitizeFileName(f.name) })));
  const [folder, setFolder] = useState(initialFolder);
  const [newFolder, setNewFolder] = useState('');
  const [target, setTarget] = useState('');
  const folders = useMemo(() => {
    const set = new Set(allFolders(listing.data?.objects ?? []));
    if (initialFolder) set.add(initialFolder);
    return [...set].sort();
  }, [listing.data, initialFolder]);
  const finalFolder = sanitizeFolder(newFolder.trim() ? joinKey(folder, newFolder) : folder);
  const total = items.reduce((s, i) => s + i.file.size, 0);

  const start = () => {
    haptic(10);
    void upload(items.map((i) => ({ file: i.file, name: i.name, folder: finalFolder, provider: target || undefined })));
    onClose();
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={`Upload ${items.length} file${items.length === 1 ? '' : 's'}`}
      footer={
        <Button variant="primary" icon="upload" className="w-full" onClick={start} disabled={!items.length}>
          Upload to {finalFolder || 'Drive'}
        </Button>
      }
    >
      <div className="space-y-5">
        <div className="space-y-2">
          {items.map((it, i) => (
            <div key={i} className="flex items-center gap-3">
              <FileTile kind={fileKind(it.name, it.file.type)} size={40} />
              <div className="min-w-0 flex-1">
                <input
                  aria-label={`Name for ${it.file.name}`}
                  value={it.name}
                  onChange={(e) => setItems((xs) => xs.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                  onBlur={(e) => setItems((xs) => xs.map((x, j) => (j === i ? { ...x, name: sanitizeFileName(e.target.value || it.file.name) } : x)))}
                  className={cx(inputClass, 'min-h-[42px]')}
                />
                <p className="px-1 pt-0.5 text-[12px] text-ink-500">
                  {formatBytes(it.file.size)}
                  {it.file.size > DIRECT_UPLOAD_THRESHOLD ? ', sent straight to storage' : ''}
                </p>
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-2">
          <span className="block px-1 text-[13px] text-ink-300">Folder</span>
          <div className="no-scrollbar -mx-5 flex gap-2 overflow-x-auto px-5">
            <Chip active={folder === ''} onClick={() => setFolder('')}>
              <Icon name="home" size={14} />
              Drive
            </Chip>
            {folders.map((f) => (
              <Chip key={f} active={folder === f} onClick={() => setFolder(f)}>
                <Icon name="folder" size={14} />
                {f}
              </Chip>
            ))}
          </div>
          <input
            aria-label="New sub-folder"
            value={newFolder}
            onChange={(e) => setNewFolder(e.target.value)}
            placeholder={`New folder inside ${folder || 'Drive'} (optional)`}
            className={inputClass}
          />
        </div>

        <Field label="Storage" hint={`${formatBytes(total)} in total. Auto picks the backend with the most free space.`}>
          <select value={target} onChange={(e) => setTarget(e.target.value)} className={inputClass}>
            <option value="">Auto</option>
            {Object.keys(capacity.data ?? {})
              .sort()
              .map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
          </select>
        </Field>
        <p className="px-1 text-[12px] text-ink-500">Names are final once uploaded: the hub has no rename or move yet, so set them here.</p>
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ per-file actions

export function FileActionsSheet({
  open,
  obj,
  onClose,
  onPreview,
  onShare,
}: {
  open: boolean;
  obj: StoredObject;
  onClose: () => void;
  onPreview: () => void;
  onShare: () => void;
}) {
  const { client, notify } = useHub();
  const confirm = useConfirm();
  const [busy, setBusy] = useState<string | null>(null);

  const download = async () => {
    let tab: Window | null = null;
    try {
      tab = window.open('', '_blank');
    } catch {
      tab = null;
    }
    setBusy('download');
    try {
      const url = await objectUrl(client, obj);
      if (tab) {
        tab.opener = null;
        tab.location.href = url;
      } else {
        const a = document.createElement('a');
        a.href = url;
        a.download = obj.key.split('/').pop() || 'download';
        a.rel = 'noopener';
        a.target = '_blank';
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
      onClose();
    } catch (err) {
      tab?.close();
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const copyLink = async () => {
    setBusy('link');
    try {
      const res = await client.drivePresign(obj.key, obj.backend, undefined, 24 * 3600);
      const ok = await copyText(res.url);
      notify(ok ? 'Link copied. It works for 24 hours.' : 'Copy failed', ok ? 'ok' : 'error');
      onClose();
    } catch (err) {
      notify(`${errorText(err)}. Share it as a drop instead.`, 'error');
    } finally {
      setBusy(null);
    }
  };

  const index = async () => {
    setBusy('index');
    try {
      const doc = await client.ragIndex(obj.backend, obj.key);
      announce('rag');
      if (doc.error) notify(`Not indexed: ${doc.error}`, 'error');
      else notify(`Ask my Drive can now read ${doc.title} (${doc.chunks} passages)`, 'ok');
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: 'Delete this file?',
      body: (
        <span>
          <strong className="text-ink-100">{obj.key}</strong> will be removed from {obj.backend}. Drops pointing at it stop working.
        </span>
      ),
      confirmLabel: 'Delete file',
      tone: 'danger',
    });
    if (!ok) return;
    setBusy('delete');
    try {
      await client.driveDelete(obj.key, obj.backend);
      announce('drive');
      notify('File deleted', 'ok');
      onClose();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const spin = (k: string) => (busy === k ? <span className="spin h-4 w-4 rounded-full border-2 border-ink-600 border-t-signal" /> : undefined);

  return (
    <Sheet open={open} onClose={onClose}>
      <div className="flex items-center gap-3 pb-4">
        <FileThumb obj={obj} size={56} />
        <div className="min-w-0 flex-1">
          <FileName objKey={obj.key} />
          <p className="truncate text-[13px] text-ink-400">
            {formatBytes(obj.size)}, {folderOf(obj.key) || 'Drive'}, {obj.backend}
          </p>
          {obj.last_modified && <p className="text-[12px] text-ink-500">{formatDateTime(obj.last_modified)}</p>}
        </div>
      </div>
      <div className="-mx-5 divide-y divide-ink-800 border-y border-ink-800">
        <ListRow icon="eye" title="Preview" onClick={onPreview} trailing={<span />} />
        <ListRow icon="share" title="Share as a drop" detail="Expiring link, optional password" onClick={onShare} trailing={<span />} />
        <ListRow icon="link" title="Copy a 24-hour link" onClick={() => void copyLink()} trailing={spin('link') ?? <span />} />
        <ListRow icon="download" title="Download" onClick={() => void download()} trailing={spin('download') ?? <span />} />
        {isIndexable(obj.key) && (
          <ListRow icon="spark" title="Add to Ask my Drive" detail="Index it so questions can cite it" onClick={() => void index()} trailing={spin('index') ?? <span />} tone="signal" />
        )}
        <ListRow icon="trash" title={<span className="text-danger-400">Delete</span>} onClick={() => void remove()} trailing={spin('delete') ?? <span />} tone="bad" />
      </div>
    </Sheet>
  );
}

// ------------------------------------------------------------------ drops

const EXPIRY = [
  { h: 1, label: '1 hour' },
  { h: 24, label: '1 day' },
  { h: 24 * 7, label: '1 week' },
  { h: 24 * 30, label: '30 days' },
];

export function DropResult({ drop }: { drop: Drop }) {
  const { notify } = useHub();
  const copy = async () => {
    const ok = await copyText(drop.url);
    haptic(8);
    notify(ok ? 'Drop link copied' : 'Copy failed', ok ? 'ok' : 'error');
  };
  const share = async () => {
    try {
      await navigator.share({ title: drop.filename, url: drop.url });
    } catch {
      // Dismissed.
    }
  };
  return (
    <div className="flex flex-col items-center gap-4 pb-2 text-center">
      <QrCode value={drop.url} size={208} />
      <p className="break-all text-[15px] text-ink-100">{drop.url}</p>
      <div className="flex flex-wrap justify-center gap-1.5">
        <Badge>expires {formatDateTime(drop.expires_at)}</Badge>
        {drop.max_downloads !== null && <Badge>{drop.max_downloads - drop.downloads} downloads left</Badge>}
        {drop.has_password && (
          <Badge tone="signal">
            <Icon name="lock" size={12} />
            password
          </Badge>
        )}
      </div>
      <div className="flex w-full gap-2">
        <Button variant="primary" icon="copy" className="flex-1" onClick={() => void copy()}>
          Copy link
        </Button>
        {typeof navigator !== 'undefined' && 'share' in navigator && (
          <Button icon="share" className="flex-1" onClick={() => void share()}>
            Share
          </Button>
        )}
      </div>
    </div>
  );
}

export function DropSheet({ open, obj, onClose }: { open: boolean; obj: StoredObject; onClose: () => void }) {
  const { client } = useHub();
  const [hours, setHours] = useState(24);
  const [limit, setLimit] = useState(false);
  const [max, setMax] = useState('5');
  const [usePw, setUsePw] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drop, setDrop] = useState<Drop | null>(null);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const d = await client.createDrop({
        provider: obj.backend,
        key: obj.key,
        expires_in_hours: hours,
        ...(limit && Number(max) > 0 ? { max_downloads: Number(max) } : {}),
        ...(usePw && password ? { password } : {}),
      });
      haptic([10, 40, 10]);
      setDrop(d);
      announce('drops');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={drop ? 'Drop ready' : 'Share as a drop'}
      footer={
        !drop && (
          <Button variant="primary" icon="share" className="w-full" onClick={() => void create()} disabled={busy || (usePw && !password)}>
            {busy ? 'Creating…' : 'Create drop link'}
          </Button>
        )
      }
    >
      {drop ? (
        <DropResult drop={drop} />
      ) : (
        <div className="space-y-5">
          <div className="flex items-center gap-3">
            <FileThumb obj={obj} size={44} />
            <div className="min-w-0">
              <FileName objKey={obj.key} />
              <p className="text-[13px] text-ink-400">{formatBytes(obj.size)}</p>
            </div>
          </div>
          <div className="space-y-2">
            <span className="block px-1 text-[13px] text-ink-300">Expires after</span>
            <div className="flex flex-wrap gap-2">
              {EXPIRY.map((e) => (
                <Chip key={e.h} active={hours === e.h} onClick={() => setHours(e.h)}>
                  {e.label}
                </Chip>
              ))}
            </div>
          </div>
          <div className="space-y-2 rounded-card bg-ink-850 p-4">
            <label className="flex min-h-[44px] items-center justify-between gap-3 text-[15px] text-ink-100">
              Limit downloads
              <Toggle checked={limit} onChange={setLimit} label="Limit downloads" />
            </label>
            {limit && <input inputMode="numeric" aria-label="Maximum downloads" value={max} onChange={(e) => setMax(e.target.value.replace(/\D/g, ''))} className={cx(inputClass, 'screen-in-up')} />}
            <label className="flex min-h-[44px] items-center justify-between gap-3 text-[15px] text-ink-100">
              Require a password
              <Toggle checked={usePw} onChange={setUsePw} label="Require a password" />
            </label>
            {usePw && (
              <input
                type="text"
                aria-label="Password"
                autoComplete="off"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Tell it to the person you share with"
                className={cx(inputClass, 'screen-in-up')}
              />
            )}
          </div>
          {error && <ErrorNote message={error} />}
        </div>
      )}
    </Sheet>
  );
}
