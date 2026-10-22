import { useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Chip, ErrorNote, Toggle, Button, cx, haptic, inputClass } from '../../components/ui';
import { announce } from '../../lib/bus';
import { formatBytes } from '../../lib/format';
import { errorText, useHub } from '../../lib/hub';
import type { FolderDrop } from '../../lib/types';
import { DropResult } from './DriveSheets';

const EXPIRY = [
  { h: 1, label: '1 hour' },
  { h: 24, label: '1 day' },
  { h: 24 * 7, label: '1 week' },
  { h: 24 * 30, label: '30 days' },
];

/** Share a whole Drive folder: a public page listing its files, with expiry, password and download limit. */
export function FolderDropSheet({ open, folder, files, size, onClose }: { open: boolean; folder: string; files: number; size: number; onClose: () => void }) {
  const { client } = useHub();
  const [hours, setHours] = useState(24 * 7);
  const [limit, setLimit] = useState(false);
  const [max, setMax] = useState('10');
  const [usePw, setUsePw] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drop, setDrop] = useState<FolderDrop | null>(null);
  const name = folder.split('/').filter(Boolean).pop() ?? folder;

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const d = await client.createFolderDrop({
        prefix: folder,
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
      title={drop ? 'Folder link ready' : 'Share folder'}
      footer={
        !drop && (
          <Button variant="primary" icon="share" className="w-full" onClick={() => void create()} disabled={busy || (usePw && !password)}>
            {busy ? 'Creating…' : 'Create folder link'}
          </Button>
        )
      }
    >
      {drop ? (
        <div className="space-y-3">
          <p className="text-center text-[14px] text-ink-400">
            {drop.file_count ?? files} file{(drop.file_count ?? files) === 1 ? '' : 's'} in {name}. Each opening of the page counts as one download; small folders get a ZIP button.
          </p>
          <DropResult drop={drop} />
        </div>
      ) : (
        <div className="space-y-5">
          <div className="flex items-center gap-3">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-[14px] bg-signal/15 text-signal">
              <Icon name="folder" size={24} />
            </span>
            <div className="min-w-0">
              <p className="truncate text-[15px] text-ink-100">{name}</p>
              <p className="text-[13px] text-ink-400">
                {files} file{files === 1 ? '' : 's'}, {formatBytes(size)}. Only these files are shared; files added to the folder later are not.
              </p>
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
              Limit openings
              <Toggle checked={limit} onChange={setLimit} label="Limit openings" />
            </label>
            {limit && <input inputMode="numeric" aria-label="Maximum openings" value={max} onChange={(e) => setMax(e.target.value.replace(/\D/g, ''))} className={cx(inputClass, 'screen-in-up')} />}
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
