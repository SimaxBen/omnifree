import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Badge, Button, ErrorNote, Spinner, cx, inputClass } from '../../components/ui';
import type { AttachmentRef, PendingAction, UsedTool } from '../../lib/assistant';
import { chipState, toolLabel, type ChipState } from '../../lib/assistant';
import { fileKind } from '../../lib/files';
import { formatBytes } from '../../lib/format';
import { errorText, useHub } from '../../lib/hub';
import type { StoredObject } from '../../lib/types';

export type ActionStatus = 'waiting' | 'running' | 'done' | 'dismissed' | 'error';
export interface ActionState extends PendingAction {
  status: ActionStatus;
  error?: string;
}

const CHIP_TONE: Record<ChipState, string> = {
  ok: 'bg-signal/10 text-signal ring-signal/30',
  created: 'bg-mint-400/10 text-mint-400 ring-mint-400/30',
  error: 'bg-danger-400/10 text-danger-400 ring-danger-400/30',
  proposed: 'bg-ink-800 text-ink-200 ring-ink-700',
  dismissed: 'bg-ink-800 text-ink-500 ring-ink-700',
  'not-created': 'bg-ink-800 text-ink-500 ring-ink-700',
};
const CHIP_PREFIX: Partial<Record<ChipState, string>> = { proposed: 'Proposed', dismissed: 'Dismissed', 'not-created': 'Not created' };

/** Small chips under an assistant reply: which of the owner's data it looked at, created or only proposed. */
export function ToolChips({ tools, routed, actions }: { tools?: UsedTool[]; routed?: 'data' | 'general'; actions?: ActionState[] }) {
  if (!tools?.length) {
    return routed === 'data' ? <Badge className="gap-1"><Icon name="spark" size={11} />your data: nothing needed</Badge> : null;
  }
  return (
    <div className="flex flex-wrap gap-1.5" aria-label="Data used for this answer">
      {tools.map((t, i) => {
        const state = chipState(t, actions);
        const prefix = CHIP_PREFIX[state];
        return (
          <span
            key={`${t.name}-${i}`}
            title={prefix ? `${prefix}: ${t.summary}` : t.summary}
            className={cx('inline-flex max-w-full items-center gap-1 truncate rounded-full px-2 py-0.5 text-[11px] ring-1 ring-inset', CHIP_TONE[state])}
          >
            <Icon name={t.name === 'remember' ? 'book' : state === 'created' ? 'check' : 'spark'} size={11} />
            {prefix && <span>{prefix}:</span>}
            <span className="font-semibold">{toolLabel(t.name)}</span>
            {t.summary && t.name !== 'remember' && <span className="truncate text-ink-300">{t.summary}</span>}
          </span>
        );
      })}
    </div>
  );
}

/** "Will create ..." card: the full summary (wrapped, never cut), then one tap to confirm. */
export function ActionCard({ action, onConfirm, onDismiss }: { action: ActionState; onConfirm: () => void; onDismiss: () => void }) {
  const done = action.status === 'done';
  const open = action.status === 'waiting' || action.status === 'error';
  return (
    <div
      className={cx(
        'space-y-2 rounded-2xl px-3 py-2.5 ring-1 ring-inset',
        done ? 'bg-mint-400/10 ring-mint-400/30' : action.status === 'dismissed' ? 'bg-ink-900 opacity-60 ring-ink-800' : 'bg-ink-900 ring-signal/40',
      )}
    >
      <div className="flex items-start gap-2">
        <Icon name={done ? 'check' : 'plus'} size={16} className={cx('mt-0.5 shrink-0', done ? 'text-mint-400' : 'text-signal')} />
        <div className="min-w-0 flex-1">
          <p className="break-words text-[14px] leading-snug text-ink-100">{action.summary}</p>
          <p className={cx('break-words text-[11px]', action.status === 'error' ? 'text-danger-400' : 'text-ink-500')}>
            {done ? 'Created' : action.status === 'dismissed' ? 'Dismissed' : action.status === 'error' ? action.error : `${toolLabel(action.tool)}: waiting for your OK`}
          </p>
        </div>
        {action.status === 'running' && <Spinner />}
      </div>
      {open && (
        <div className="flex items-center justify-end gap-2">
          <Button variant="ghost" icon="x" onClick={onDismiss}>
            Dismiss
          </Button>
          <Button variant="primary" icon="check" onClick={onConfirm} className="flex-1">
            Confirm
          </Button>
        </div>
      )}
    </div>
  );
}

export interface DraftAttachment {
  id: number;
  name: string;
  size: number;
  progress: number;
  ref?: AttachmentRef;
  error?: string;
}

/** Files attached to the message being written (uploading, ready or failed), or to a sent one. */
export function AttachmentChips({ items, onRemove }: { items: (DraftAttachment | AttachmentRef)[]; onRemove?: (index: number) => void }) {
  if (!items.length) return null;
  // Why an upload failed is written out: a hover title never shows on a phone.
  const failed = items.filter((a): a is DraftAttachment => 'progress' in a && !!a.error);
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap gap-1.5">
        {items.map((a, i) => {
          const draft = 'progress' in a ? a : null;
          const ref = draft ? draft.ref : (a as AttachmentRef);
          const name = draft ? draft.name : (ref?.name ?? ref?.key.split('/').pop() ?? 'file');
          const kind = fileKind(name, ref?.content_type);
          return (
            <span
              key={draft ? draft.id : `${ref?.provider}:${ref?.key}`}
              className={cx(
                'inline-flex max-w-[220px] items-center gap-1.5 rounded-full bg-ink-800 py-1 pl-2.5 pr-1.5 text-[12px] ring-1 ring-inset',
                draft?.error ? 'text-danger-400 ring-danger-400/40' : 'text-ink-200 ring-ink-700',
              )}
              title={draft?.error ?? name}
            >
              <Icon name={kind === 'image' ? 'camera' : 'file'} size={12} />
              <span className="truncate">{name}</span>
              {draft && !draft.ref && !draft.error && <span className="font-mono text-ink-500">{Math.round(draft.progress * 100)}%</span>}
              {ref?.pages ? <span className="text-ink-500">{ref.pages} p</span> : null}
              {onRemove && (
                <button type="button" aria-label={`Remove ${name}`} onClick={() => onRemove(i)} className="press grid h-5 w-5 place-items-center rounded-full text-ink-400 hover:text-ink-100">
                  <Icon name="x" size={12} />
                </button>
              )}
            </span>
          );
        })}
      </div>
      {failed.map((d) => (
        <p key={d.id} role="alert" className="break-words text-[12px] text-danger-400">
          {d.name}: {d.error} (remove it and attach it again)
        </p>
      ))}
    </div>
  );
}

/** Attach sheet: a photo/file from the phone, or an existing Drive file (searched by name). */
export function AttachSheet({
  open,
  onClose,
  onPickFiles,
  onPickDrive,
}: {
  open: boolean;
  onClose: () => void;
  onPickFiles: (files: File[]) => void;
  onPickDrive: (obj: StoredObject) => void;
}) {
  const { client } = useHub();
  const fileRef = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState('');
  const [items, setItems] = useState<StoredObject[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const ctrl = new AbortController();
    const t = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      client
        .driveList('', q.trim(), ctrl.signal)
        .then((l) => {
          const usable = l.objects.filter((o) => ['image', 'pdf', 'text'].includes(fileKind(o.key, o.content_type)));
          usable.sort((a, b) => (b.last_modified ?? '').localeCompare(a.last_modified ?? ''));
          setItems(usable.slice(0, 40));
        })
        .catch((err) => {
          if (!(err instanceof DOMException && err.name === 'AbortError')) setError(errorText(err));
        })
        .finally(() => setLoading(false));
    }, 250);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [open, q, client]);

  return (
    <Sheet open={open} onClose={onClose} title="Attach" full>
      <div className="space-y-3">
        <Button variant="primary" icon="upload" className="w-full" onClick={() => fileRef.current?.click()}>
          Photo or file from this phone
        </Button>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept="image/*,application/pdf,text/plain,text/markdown,text/csv,application/json"
          className="hidden"
          aria-label="Choose files"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = '';
            if (files.length) onPickFiles(files);
          }}
        />
        <p className="pt-2 font-display text-[14px] font-semibold text-ink-400">From your Drive</p>
        <input className={inputClass} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search images, PDFs, text files" aria-label="Search Drive" />
        {error && <ErrorNote message={error} />}
        {loading && !items && <Spinner />}
        {items && items.length === 0 && <p className="text-sm text-ink-500">No image, PDF or text file matches.</p>}
        <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
          {items?.map((o) => (
            <button
              key={`${o.backend}:${o.key}`}
              type="button"
              onClick={() => onPickDrive(o)}
              className="press flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-ink-850"
            >
              <Icon name={fileKind(o.key, o.content_type) === 'image' ? 'camera' : 'file'} size={18} className="text-ink-400" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[14px] text-ink-100">{o.key.split('/').pop()}</span>
                <span className="block truncate text-[12px] text-ink-500">
                  {o.key} · {formatBytes(o.size)}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </Sheet>
  );
}
