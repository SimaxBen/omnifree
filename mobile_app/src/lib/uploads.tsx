import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { DIRECT_UPLOAD_THRESHOLD } from './api';
import { announce } from './bus';
import { joinKey, sanitizeFileName } from './files';
import { errorText, useHub } from './hub';

export interface UploadJob {
  id: number;
  name: string;
  key: string;
  progress: number;
  /** Sent straight to the storage backend through a presigned URL. */
  direct: boolean;
  error?: string;
  done?: boolean;
}

export interface UploadRequest {
  file: File;
  /** Final file name (sanitised again here). */
  name?: string;
  folder?: string;
  provider?: string;
}

interface UploadsValue {
  jobs: UploadJob[];
  active: number;
  /** Overall progress of running uploads, 0..1 (null when idle). */
  overall: number | null;
  upload: (reqs: UploadRequest[]) => Promise<void>;
  dismiss: (id: number) => void;
}

const UploadsContext = createContext<UploadsValue | null>(null);

/** Uploads live above the screens, so they keep going (and stay visible) while you switch tabs. */
export function UploadsProvider({ children }: { children: ReactNode }) {
  const { client, notify } = useHub();
  const [jobs, setJobs] = useState<UploadJob[]>([]);
  const nextId = useRef(1);

  const patch = (id: number, p: Partial<UploadJob>) => setJobs((js) => js.map((j) => (j.id === id ? { ...j, ...p } : j)));

  const upload = useCallback(
    async (reqs: UploadRequest[]) => {
      for (const req of reqs) {
        const name = sanitizeFileName(req.name || req.file.name);
        const key = joinKey(req.folder ?? '', name);
        const id = nextId.current++;
        setJobs((js) => [...js, { id, name, key, progress: 0, direct: req.file.size > DIRECT_UPLOAD_THRESHOLD }]);
        try {
          const res = await client.driveUploadFile(req.file, {
            key,
            provider: req.provider || undefined,
            onProgress: (p) => patch(id, { progress: p }),
          });
          patch(id, { progress: 1, done: true });
          window.setTimeout(() => setJobs((js) => js.filter((j) => j.id !== id)), 1600);
          notify(`Uploaded ${name}${res.provider ? ` to ${res.provider}` : ''}`, 'ok');
        } catch (err) {
          patch(id, { error: errorText(err) });
        }
      }
      announce('drive');
    },
    [client, notify],
  );

  const dismiss = useCallback((id: number) => setJobs((js) => js.filter((j) => j.id !== id)), []);

  const value = useMemo(() => {
    const running = jobs.filter((j) => !j.error && !j.done);
    const overall = running.length ? running.reduce((s, j) => s + j.progress, 0) / running.length : null;
    return { jobs, active: running.length, overall, upload, dismiss };
  }, [jobs, upload, dismiss]);

  return <UploadsContext.Provider value={value}>{children}</UploadsContext.Provider>;
}

export function useUploads(): UploadsValue {
  const ctx = useContext(UploadsContext);
  if (!ctx) throw new Error('useUploads must be used inside <UploadsProvider>');
  return ctx;
}
