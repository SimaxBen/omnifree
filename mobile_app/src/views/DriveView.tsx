import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import { Icon } from '../components/icons';
import { Badge, Button, Card, EmptyState, ErrorNote, IconButton, QuotaBar, SectionTitle, Spinner, cx, inputClass } from '../components/ui';
import { formatBytes, formatDateTime } from '../lib/format';
import { errorText, useHub, useHubData, useHubEvents } from '../lib/hub';
import type { StoredObject } from '../lib/types';

interface Upload {
  id: number;
  name: string;
  progress: number;
  error?: string;
}

export default function DriveView() {
  const { client, notify } = useHub();
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [target, setTarget] = useState('');
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query.trim()), 300);
    return () => window.clearTimeout(t);
  }, [query]);

  const capacity = useHubData((c, s) => c.driveCapacity(s), [], 120_000);
  const listing = useHubData((c, s) => c.driveList('', debounced, s), [debounced]);

  useHubEvents(['storage.*'], () => {
    listing.reload();
    capacity.reload();
  });

  const backends = Object.keys(capacity.data ?? {}).sort();

  const files = useMemo(() => {
    const needle = debounced.toLowerCase();
    const all = listing.data?.objects ?? [];
    const filtered = needle ? all.filter((o) => o.key.toLowerCase().includes(needle)) : all;
    return [...filtered].sort((a, b) => (b.last_modified ?? '').localeCompare(a.last_modified ?? '') || a.key.localeCompare(b.key));
  }, [listing.data, debounced]);

  const onFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = '';
    for (const file of picked) {
      const id = nextId.current++;
      setUploads((u) => [...u, { id, name: file.name, progress: 0 }]);
      try {
        const obj = await client.driveUpload(file, {
          key: file.name,
          provider: target || undefined,
          onProgress: (p) => setUploads((u) => u.map((x) => (x.id === id ? { ...x, progress: p } : x))),
        });
        setUploads((u) => u.filter((x) => x.id !== id));
        notify(`Stored ${obj.key ?? file.name} on ${obj.backend ?? 'the pool'}`, 'ok');
      } catch (err) {
        setUploads((u) => u.map((x) => (x.id === id ? { ...x, error: errorText(err) } : x)));
      }
    }
    listing.reload();
    capacity.reload();
  };

  const download = async (obj: StoredObject) => {
    setBusyKey(`${obj.backend}/${obj.key}`);
    try {
      const blob = await client.driveDownload(obj.key, obj.backend);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = obj.key.split('/').pop() || 'download';
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusyKey(null);
    }
  };

  const remove = async (obj: StoredObject) => {
    if (!window.confirm(`Delete ${obj.key} from ${obj.backend}?`)) return;
    setBusyKey(`${obj.backend}/${obj.key}`);
    try {
      await client.driveDelete(obj.key, obj.backend);
      listing.setData((prev) => prev && { ...prev, objects: prev.objects.filter((o) => !(o.key === obj.key && o.backend === obj.backend)) });
      capacity.reload();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusyKey(null);
    }
  };

  const errors = Object.entries(listing.data?.errors ?? {});

  return (
    <div className="space-y-5">
      <div>
        <SectionTitle action={capacity.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh capacity" onClick={capacity.reload} />}>
          Storage pool
        </SectionTitle>
        {capacity.error && <ErrorNote message={capacity.error} onRetry={capacity.reload} />}
        <Card className="space-y-3 p-3">
          {backends.length === 0 && !capacity.loading && <p className="text-sm text-ink-400">No storage backends reported.</p>}
          {backends.map((name) => (
            <QuotaBar key={name} quota={capacity.data![name]} label={name} />
          ))}
        </Card>
      </div>

      <Card className="space-y-2 p-3">
        <div className="flex gap-2">
          <select aria-label="Upload target" className={cx(inputClass, 'flex-1')} value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">auto (most free space)</option>
            {backends.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
          <Button variant="primary" icon="upload" onClick={() => fileRef.current?.click()}>
            Upload
          </Button>
          <input ref={fileRef} type="file" multiple className="hidden" onChange={onFiles} />
        </div>
        {uploads.map((u) => (
          <div key={u.id} className="space-y-1 text-xs">
            <div className="flex justify-between gap-2">
              <span className="truncate text-ink-200">{u.name}</span>
              {u.error ? (
                <button type="button" className="text-ink-400 underline" onClick={() => setUploads((x) => x.filter((y) => y.id !== u.id))}>
                  dismiss
                </button>
              ) : (
                <span className="font-mono text-ink-400">{Math.round(u.progress * 100)}%</span>
              )}
            </div>
            {u.error ? (
              <p className="text-danger-400">{u.error}</p>
            ) : (
              <div className="h-1 overflow-hidden rounded-full bg-ink-800">
                <div className="h-full bg-signal transition-[width]" style={{ width: `${u.progress * 100}%` }} />
              </div>
            )}
          </div>
        ))}
      </Card>

      <div>
        <div className="relative mb-3">
          <Icon name="search" size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-500" />
          <input
            type="search"
            aria-label="Search files"
            className={cx(inputClass, 'pl-9')}
            placeholder="Search all backends"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <SectionTitle action={listing.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh files" onClick={listing.reload} />}>
          Files {files.length > 0 && <span className="text-ink-500">({files.length})</span>}
        </SectionTitle>
        {listing.error && <ErrorNote message={listing.error} onRetry={listing.reload} />}
        {errors.length > 0 && (
          <p className="mb-2 text-xs text-amber-400">Unreachable: {errors.map(([k, v]) => `${k} (${v})`).join(', ')}</p>
        )}
        {!listing.loading && !listing.error && files.length === 0 && (
          <EmptyState icon="drive" title={debounced ? 'No matching files' : 'Drive is empty'} hint={debounced ? undefined : 'Uploads go to the backend with the most free space.'} />
        )}
        {files.length > 0 && (
        <ul className="divide-y divide-ink-800 overflow-hidden rounded-xl border border-ink-700/70 bg-ink-900/80">
          {files.map((o) => {
            const k = `${o.backend}/${o.key}`;
            return (
              <li key={k} className="flex items-center gap-2 px-3 py-2">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-ink-100" title={o.key}>
                    {o.key}
                  </p>
                  <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-500">
                    <Badge>{o.backend}</Badge>
                    <span className="font-mono">{formatBytes(o.size)}</span>
                    {o.last_modified && <span>{formatDateTime(o.last_modified)}</span>}
                  </p>
                </div>
                {busyKey === k ? (
                  <Spinner className="mx-3" />
                ) : (
                  <>
                    <IconButton icon="download" label={`Download ${o.key}`} onClick={() => void download(o)} />
                    <IconButton icon="trash" label={`Delete ${o.key}`} onClick={() => void remove(o)} className="hover:text-danger-400" />
                  </>
                )}
              </li>
            );
          })}
        </ul>
        )}
      </div>
    </div>
  );
}
