// Shared previewer states: loading, error, "too big" and "no preview", each with the fallback
// actions (open externally / download), plus the hook that loads a source's bytes.

import { useEffect, useState, type ReactNode } from 'react';
import { formatBytes } from '../../lib/format';
import { errorText, useHub } from '../../lib/hub';
import { CorsBlockedError, TooBigError, type PreviewSource } from '../../lib/preview/source';
import { Spinner, cx } from '../ui';
import { Icon, type IconName } from '../icons';

export function Loading({ label = 'Loading preview', className }: { label?: string; className?: string }) {
  return (
    <div className={cx('grid h-full place-items-center p-6', className)}>
      <div className="flex flex-col items-center gap-3 text-[13px] text-ink-400">
        <Spinner className="h-6 w-6" />
        <span>{label}</span>
      </div>
    </div>
  );
}

export function FallbackActions({ src, primary = false }: { src: PreviewSource; primary?: boolean }) {
  const { notify } = useHub();
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await src.download();
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      onClick={run}
      disabled={busy}
      className={cx(
        'press inline-flex min-h-[44px] items-center justify-center gap-2 rounded-full px-5 text-[15px] disabled:opacity-50',
        primary ? 'bg-signal font-semibold text-ink-950' : 'bg-ink-800 text-ink-100',
      )}
    >
      {busy ? <Spinner /> : <Icon name={src.obj ? 'external' : 'download'} size={18} />}
      {src.obj ? 'Open externally / Download' : 'Download this file'}
    </button>
  );
}

export function PreviewMessage({ src, icon = 'file', title, children }: { src: PreviewSource; icon?: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="grid h-full place-items-center overflow-y-auto p-8 text-center">
      <div className="max-w-sm space-y-4">
        <span className="mx-auto grid h-20 w-20 place-items-center rounded-[22px] bg-ink-850 text-ink-300 ring-1 ring-inset ring-ink-700">
          <Icon name={icon} size={34} />
        </span>
        <p className="font-display text-[18px] font-semibold text-ink-100">{title}</p>
        {children && <div className="text-[14px] leading-relaxed text-ink-400">{children}</div>}
        <FallbackActions src={src} primary />
      </div>
    </div>
  );
}

/** The right message for a failed load: too big, storage CORS, or the error itself. */
export function PreviewError({ src, error }: { src: PreviewSource; error: unknown }) {
  if (error instanceof TooBigError) {
    return (
      <PreviewMessage src={src} icon="file" title="Too large to preview here">
        This file is {formatBytes(error.size)}; previews stop at {formatBytes(error.limit)} to keep the phone responsive.
      </PreviewMessage>
    );
  }
  if (error instanceof CorsBlockedError) {
    return (
      <PreviewMessage src={src} icon="lock" title="Can't read this file in the app">
        The storage backend does not allow the app to read it directly (CORS rule missing). It still opens outside the app.
      </PreviewMessage>
    );
  }
  return (
    <PreviewMessage src={src} icon="file" title="Couldn't preview this file">
      {typeof error === 'string' ? error : errorText(error)}
    </PreviewMessage>
  );
}

export interface BytesState {
  data: ArrayBuffer | null;
  error: unknown;
}

/** Loads the whole file (at most `max` bytes). */
export function useSourceBytes(src: PreviewSource, max: number): BytesState {
  const [state, setState] = useState<BytesState>({ data: null, error: null });
  useEffect(() => {
    const ctrl = new AbortController();
    setState({ data: null, error: null });
    src.bytes(max, ctrl.signal).then(
      (data) => !ctrl.signal.aborted && setState({ data, error: null }),
      (error) => !ctrl.signal.aborted && setState({ data: null, error }),
    );
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src.id, max]);
  return state;
}

/** UTF-8 (with BOM) when valid, else Windows-1252 (spreadsheets exported by Excel in French locales). */
export function decodeText(data: ArrayBuffer): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(data);
  } catch {
    return new TextDecoder('windows-1252').decode(data);
  }
}
