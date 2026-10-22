import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Button, Chip, ErrorNote, Spinner, cx, haptic, inputClass } from '../../components/ui';
import { announce } from '../../lib/bus';
import { formatBytes } from '../../lib/format';
import { errorText, useHub } from '../../lib/hub';
import {
  TYPE_ICON,
  TYPE_LABEL,
  looksLikeUrl,
  savedFileKey,
  type SaveRequest,
  type SaveResult,
  type SavedCollection,
  type SavedItem,
  type SavedPreviewResult,
} from '../../lib/saved';
import { CollectionDot, SavedThumb } from './parts';
import { moveItem } from './SavedItemSheet';

const PREVIEW_DEBOUNCE_MS = 600;

/** "+" on the Saved screen: paste a link or type text (live preview of a link), or pick an image / PDF. */
export default function SaveSheet({
  open,
  onClose,
  collections,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  collections: SavedCollection[];
  onSaved: (item: SavedItem) => void;
}) {
  const { client, notify } = useHub();
  const [text, setText] = useState('');
  const [note, setNote] = useState('');
  const [showNote, setShowNote] = useState(false);
  const [collection, setCollection] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<{ loading: boolean; data?: SavedPreviewResult; error?: string }>({ loading: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SaveResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const url = looksLikeUrl(text) ? text.trim() : '';

  useEffect(() => {
    if (!open) return;
    setText('');
    setNote('');
    setShowNote(false);
    setCollection(null);
    setFile(null);
    setPreview({ loading: false });
    setError(null);
    setResult(null);
  }, [open]);

  useEffect(() => {
    if (!url) {
      setPreview({ loading: false });
      return undefined;
    }
    const ctrl = new AbortController();
    setPreview({ loading: true });
    const t = window.setTimeout(() => {
      client
        .savedPreview(url, ctrl.signal)
        .then((data) => setPreview({ loading: false, data }))
        .catch((err) => {
          if (!ctrl.signal.aborted) setPreview({ loading: false, error: errorText(err) });
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [url, client]);

  const paste = async () => {
    try {
      const clip = await navigator.clipboard.readText();
      if (clip.trim()) {
        setText(clip.trim());
        setFile(null);
      }
    } catch {
      notify('Clipboard not available: long-press the box and paste', 'info');
    }
  };

  const save = async () => {
    if (busy) return;
    haptic(10);
    setBusy(true);
    setError(null);
    try {
      const base: SaveRequest = {
        ...(note.trim() ? { note: note.trim() } : {}),
        ...(collection ? { collection_id: collection } : {}),
      };
      let req: SaveRequest;
      if (file) {
        const up = await client.driveUploadFile(file, { key: savedFileKey(file.name) });
        req = { ...base, key: up.key, ...(up.provider ? { provider: up.provider } : {}) };
      } else if (url) {
        req = { ...base, url };
      } else {
        req = { ...base, text: text.trim() };
      }
      const res = await client.savedSave(req);
      setResult(res);
      announce('saved');
      onSaved(res.item);
      notify(res.duplicate ? 'Already saved: moved to the top' : 'Saved', 'ok');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const accept = async (name: string) => {
    if (!result) return;
    try {
      const item = await moveItem(client, result.item, { name });
      setResult({ ...result, item });
      onSaved(item);
      notify(`Added to ${name}`, 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const canSave = !!file || !!text.trim();
  const existing = preview.data?.existing;
  const p = preview.data?.preview;

  return (
    <Sheet open={open} onClose={onClose} title={result ? (result.duplicate ? 'Already saved' : 'Saved') : 'Save'}>
      {result ? (
        <div className="space-y-4 pb-2">
          <div className="flex items-start gap-3 rounded-card bg-ink-850 p-3 ring-1 ring-inset ring-ink-700">
            <SavedThumb item={result.item} size={64} />
            <div className="min-w-0 flex-1">
              <p className="line-clamp-2 font-display text-[16px] font-semibold leading-snug text-ink-100">{result.item.title}</p>
              <p className="mt-0.5 flex items-center gap-1.5 text-[12px] text-ink-400">
                <Icon name={TYPE_ICON[result.item.type] ?? 'bookmark'} size={13} />
                {result.item.site || TYPE_LABEL[result.item.type]}
              </p>
              {result.item.summary && <p className="mt-1.5 line-clamp-3 text-[13px] leading-snug text-ink-300">{result.item.summary}</p>}
            </div>
          </div>
          {result.item.suggestion && !result.item.collection_id && (
            <button
              type="button"
              onClick={() => void accept(result.item.suggestion!.name)}
              className="press flex min-h-[52px] w-full items-center gap-2 rounded-row bg-signal/10 px-4 text-left text-[15px] text-signal ring-1 ring-inset ring-signal/30"
            >
              <Icon name="spark" size={18} />
              <span className="min-w-0 flex-1 truncate">
                Add to <b>{result.item.suggestion.name}</b>
                {result.item.suggestion.new ? ' (new)' : ''}
              </span>
              <span className="text-[13px] font-semibold">Accept</span>
            </button>
          )}
          {result.item.collection_id && (
            <p className="flex items-center gap-2 text-[14px] text-ink-300">
              <CollectionDot color={collections.find((c) => c.id === result.item.collection_id)?.color} />
              In {collections.find((c) => c.id === result.item.collection_id)?.name ?? 'a collection'}
            </p>
          )}
          <div className="flex gap-2">
            <Button variant="subtle" className="flex-1" onClick={() => setResult(null)} icon="plus">
              Save another
            </Button>
            <Button variant="primary" className="flex-1" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4 pb-2">
          {file ? (
            <div className="flex items-center gap-3 rounded-row bg-ink-850 px-3 py-3 ring-1 ring-inset ring-ink-700">
              <Icon name={file.type === 'application/pdf' ? 'file' : 'camera'} size={20} className="shrink-0 text-ink-300" />
              <span className="min-w-0 flex-1 truncate text-[15px] text-ink-100">{file.name}</span>
              <span className="shrink-0 text-[12px] text-ink-500">{formatBytes(file.size)}</span>
              <button type="button" aria-label="Remove file" onClick={() => setFile(null)} className="press grid h-9 w-9 place-items-center rounded-full text-ink-400 hover:bg-ink-800">
                <Icon name="x" size={18} />
              </button>
            </div>
          ) : (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={url ? 2 : 4}
              maxLength={20000}
              autoFocus
              placeholder="Paste a link, or type a quote or an idea…"
              aria-label="Link or text to save"
              className={cx(inputClass, 'min-h-[64px] py-3 leading-snug')}
            />
          )}

          {!file && !text && (
            <div className="grid grid-cols-2 gap-2">
              <Button icon="copy" onClick={() => void paste()}>
                Paste
              </Button>
              <Button icon="upload" onClick={() => fileRef.current?.click()}>
                Image or PDF
              </Button>
            </div>
          )}
          <input
            ref={fileRef}
            type="file"
            accept="image/*,application/pdf,.pdf"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) {
                setFile(f);
                setText('');
              }
              e.target.value = '';
            }}
          />

          {url && (
            <div className="min-h-[76px] rounded-card bg-ink-850 p-3 ring-1 ring-inset ring-ink-700" aria-live="polite">
              {preview.loading && (
                <div className="flex items-center gap-3">
                  <div className="skeleton h-14 w-14 shrink-0 rounded-[14px]" />
                  <div className="flex-1 space-y-2">
                    <div className="skeleton h-3.5 w-4/5" />
                    <div className="skeleton h-3 w-1/3" />
                  </div>
                </div>
              )}
              {existing && (
                <div className="flex items-center gap-3">
                  <SavedThumb item={existing} />
                  <div className="min-w-0 flex-1">
                    <p className="line-clamp-2 text-[15px] font-medium text-ink-100">{existing.title}</p>
                    <p className="text-[12px] text-amber-400">Already in your library: saving moves it to the top</p>
                  </div>
                </div>
              )}
              {p && (
                <div className="flex items-center gap-3">
                  <SavedThumb item={{ thumb: p.image, type: p.type }} />
                  <div className="min-w-0 flex-1">
                    <p className="line-clamp-2 text-[15px] font-medium text-ink-100">{p.title || url}</p>
                    <p className="flex items-center gap-1.5 truncate text-[12px] text-ink-400">
                      <Icon name={TYPE_ICON[p.type] ?? 'bookmark'} size={13} />
                      {p.site_name || p.site}
                      {p.status === 'pending' && <span className="text-amber-400"> · could not read the page, the link still saves</span>}
                    </p>
                  </div>
                </div>
              )}
              {preview.error && <p className="text-[13px] text-ink-400">No preview ({preview.error}). The link can still be saved.</p>}
            </div>
          )}

          {collections.length > 0 && (
            <div>
              <p className="px-1 pb-1.5 text-[13px] text-ink-400">Collection (optional, a suggestion comes with the save)</p>
              <div className="no-scrollbar -mx-4 flex gap-2 overflow-x-auto px-4">
                <Chip active={!collection} onClick={() => setCollection(null)}>
                  Auto
                </Chip>
                {collections.map((c) => (
                  <Chip key={c.id} active={collection === c.id} onClick={() => setCollection(collection === c.id ? null : c.id)}>
                    <CollectionDot color={c.color} />
                    {c.emoji ? `${c.emoji} ` : ''}
                    {c.name}
                  </Chip>
                ))}
              </div>
            </div>
          )}

          {showNote ? (
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              maxLength={4000}
              placeholder="Your note (optional)"
              aria-label="Note"
              className={cx(inputClass, 'py-3')}
            />
          ) : (
            <button type="button" onClick={() => setShowNote(true)} className="press min-h-[40px] px-1 text-[14px] text-signal">
              + Add a note
            </button>
          )}

          {error && <ErrorNote message={error} />}

          <Button variant="primary" className="w-full" disabled={!canSave || busy} onClick={() => void save()}>
            {busy ? (
              <>
                <Spinner className="border-ink-950/30 border-t-ink-950" />
                {file ? 'Uploading and reading…' : 'Saving, reading the page…'}
              </>
            ) : (
              <>
                <Icon name="bookmark" size={18} />
                Save
              </>
            )}
          </Button>
        </div>
      )}
    </Sheet>
  );
}
