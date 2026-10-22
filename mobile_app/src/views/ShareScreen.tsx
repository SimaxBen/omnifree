import { useEffect, useState } from 'react';
import { Icon, type IconName } from '../components/icons';
import QuickTaskSheet from '../components/QuickTaskSheet';
import { Button, Card, cx, haptic, riseStyle } from '../components/ui';
import { useActions } from '../lib/actions';
import { announce } from '../lib/bus';
import { copyText } from '../lib/dom';
import { formatBytes } from '../lib/format';
import { errorText, useHub } from '../lib/hub';
import { writePref } from '../lib/settings';
import { useNav } from '../lib/nav';
import {
  aiPromptOf,
  clearShared,
  isAudioFile,
  isReceiptFile,
  noteBodyOf,
  readShared,
  shareActions,
  sharedFile,
  sharedText,
  sharedUrl,
  taskTitleOf,
  type ShareAction,
  type SharedPayload,
} from '../lib/share';
import type { ShortLink } from '../lib/types';

const ACTIONS: Record<ShareAction, { icon: IconName; label: string; hint: string; tone: string }> = {
  receipt: { icon: 'receipt', label: 'Expense', hint: 'Read the receipt into an expense', tone: 'bg-mint-400/15 text-mint-400' },
  voice: { icon: 'mic', label: 'Voice note', hint: 'Transcribe into a note or tasks', tone: 'bg-danger-400/15 text-danger-400' },
  drive: { icon: 'upload', label: 'Drive', hint: 'Upload to Drive / Shared', tone: 'bg-[#7FA8FF]/15 text-[#A9CBFF]' },
  link: { icon: 'link', label: 'Short link', hint: 'Shorten and copy the link', tone: 'bg-signal/15 text-signal' },
  note: { icon: 'edit', label: 'Note', hint: 'Keep it as a note', tone: 'bg-signal/15 text-signal' },
  task: { icon: 'check', label: 'Task', hint: 'Turn it into a task', tone: 'bg-mint-400/15 text-mint-400' },
  ai: { icon: 'spark', label: 'Ask AI', hint: 'Start a chat with it', tone: 'bg-[#C9A7FF]/15 text-[#D9C2FF]' },
};

/** The Android share sheet lands here (public/share-sw.js stashed the payload): pick what to do with it. */
export default function ShareScreen({ onClose }: { onClose: (after?: () => void) => void }) {
  const { client, notify } = useHub();
  const actions = useActions();
  const { route, go } = useNav();
  const [payload, setPayload] = useState<SharedPayload | null | undefined>(undefined);
  const [busy, setBusy] = useState<ShareAction | null>(null);
  const [link, setLink] = useState<ShortLink | null>(null);
  const [taskOpen, setTaskOpen] = useState(false);
  const failed = /[?&]error=1/.test(window.location.hash);

  useEffect(() => {
    readShared()
      .then(setPayload)
      .catch(() => setPayload(null));
  }, []);

  const finish = (after?: () => void) => {
    void clearShared();
    onClose(after);
  };

  const files = async (): Promise<File[]> => {
    const out = await Promise.all((payload?.files ?? []).map((f) => sharedFile(f)));
    const ok = out.filter((f): f is File => !!f);
    if (!ok.length) throw new Error('The shared file is no longer available: share it again');
    return ok;
  };

  const run = async (a: ShareAction) => {
    if (!payload || busy) return;
    haptic(8);
    if (a === 'task') {
      setTaskOpen(true);
      return;
    }
    setBusy(a);
    try {
      if (a === 'note') {
        const body = noteBodyOf(payload);
        finish(() => actions.newNote({ body }));
      } else if (a === 'ai') {
        const prompt = aiPromptOf(payload);
        // A fresh conversation with the shared text in its composer. ChatPanel reads both prefs when it mounts, so
        // when the AI tab is already showing (the restored last tab) leave it first, as SearchScreen does.
        const openChat = () => {
          writePref('chat.current', '');
          writePref('chat.draft', prompt);
          go('ai');
        };
        finish(() => {
          if (route.tab === 'ai') {
            go('home');
            window.setTimeout(openChat, 0);
          } else openChat();
        });
      } else if (a === 'receipt') {
        const [file] = await files();
        finish(() => actions.scanReceiptFile(file));
      } else if (a === 'drive') {
        const list = await files();
        finish(() => actions.uploadFiles(list, 'Shared'));
      } else if (a === 'voice') {
        const [file] = await files();
        const res = await client.voiceNote(file, { mode: 'auto', filename: file.name });
        announce('notes', 'tasks');
        notify(res.note ? 'Voice note saved' : `${res.tasks.length} task${res.tasks.length === 1 ? '' : 's'} added`, 'ok');
        finish(() => (res.note ? actions.openNote(res.note) : go('notes', 'tasks')));
      } else if (a === 'link') {
        const made = await client.createLink({ url: sharedUrl(payload), ...(payload.title.trim() ? { title: payload.title.trim().slice(0, 300) } : {}) });
        announce('links');
        setLink(made);
        const copied = await copyText(made.short_url);
        notify(copied ? 'Short link copied' : 'Short link created', 'ok');
      }
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setBusy(null);
    }
  };

  const url = payload ? sharedUrl(payload) : '';
  const text = payload ? sharedText(payload) : '';

  return (
    <div className="pt-safe fixed inset-0 z-40 flex flex-col bg-ink-950" role="dialog" aria-modal="true" aria-label="Shared to OmniFree">
      <div className="mx-auto flex h-14 w-full max-w-xl shrink-0 items-center gap-1 px-2">
        <button type="button" onClick={() => finish()} aria-label="Close" className="press grid h-11 w-11 place-items-center rounded-full text-ink-200 hover:bg-ink-800">
          <Icon name="x" size={22} />
        </button>
        <p className="min-w-0 flex-1 truncate font-display text-[17px] font-semibold text-ink-100">Shared to OmniFree</p>
      </div>
      <div className="scroll-thin mx-auto w-full max-w-xl flex-1 space-y-5 overflow-y-auto px-4 pb-10">
        {payload === undefined && <div className="skeleton h-24" />}
        {payload === null && (
          <Card className="space-y-3 p-4 text-center">
            <p className="text-[15px] text-ink-200">{failed ? 'The shared content could not be read.' : 'Nothing is waiting here.'}</p>
            <p className="text-[13px] text-ink-400">Share text, a link or files from another app and pick OmniFree.</p>
            <Button variant="primary" onClick={() => finish()}>
              Back to the app
            </Button>
          </Card>
        )}
        {payload && (
          <>
            <Card className="space-y-2 p-4">
              {payload.title.trim() && <p className="font-display text-[17px] font-semibold text-ink-100">{payload.title}</p>}
              {text && <p className="line-clamp-6 whitespace-pre-line break-words text-[14px] text-ink-300">{text}</p>}
              {url && <p className="truncate font-mono text-[12px] text-signal">{url}</p>}
              {payload.files.map((f) => (
                <div key={f.path} className="flex items-center gap-3 rounded-row bg-ink-850 px-3 py-2">
                  <Icon name={isAudioFile(f) ? 'mic' : isReceiptFile(f) ? 'camera' : 'file'} size={18} className="shrink-0 text-ink-300" />
                  <span className="min-w-0 flex-1 truncate text-[14px] text-ink-100">{f.name}</span>
                  <span className="shrink-0 text-[12px] text-ink-500">{formatBytes(f.size)}</span>
                </div>
              ))}
            </Card>

            {link && (
              <Card className="flex items-center gap-2 p-3">
                <span className="min-w-0 flex-1 truncate font-mono text-[14px] text-ink-100">{link.short_url}</span>
                <Button icon="copy" onClick={() => void copyText(link.short_url).then((ok) => notify(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'error'))}>
                  Copy
                </Button>
                <Button variant="ghost" onClick={() => finish()}>
                  Done
                </Button>
              </Card>
            )}

            <div className="grid grid-cols-2 gap-3">
              {shareActions(payload).map((a, i) => {
                const meta = ACTIONS[a];
                return (
                  <button
                    key={a}
                    type="button"
                    onClick={() => void run(a)}
                    disabled={!!busy}
                    style={riseStyle(i)}
                    className="press rise flex min-h-[120px] flex-col items-start justify-between rounded-card bg-ink-850 p-4 text-left ring-1 ring-inset ring-ink-700 active:bg-ink-800 disabled:opacity-50"
                  >
                    <span className={cx('grid h-11 w-11 place-items-center rounded-full', meta.tone)}>
                      {busy === a ? <span className="spin h-5 w-5 rounded-full border-2 border-current border-t-transparent" /> : <Icon name={meta.icon} size={22} />}
                    </span>
                    <span>
                      <span className="block font-display text-[16px] font-semibold text-ink-100">{meta.label}</span>
                      <span className="block text-[12px] leading-snug text-ink-400">{meta.hint}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          </>
        )}
      </div>
      {payload && taskOpen && (
        <QuickTaskSheet
          open={taskOpen}
          onClose={() => setTaskOpen(false)}
          initialTitle={taskTitleOf(payload)}
          details={url && taskTitleOf(payload) !== url ? url : undefined}
          onCreated={() => finish(() => go('notes', 'tasks'))}
        />
      )}
    </div>
  );
}
