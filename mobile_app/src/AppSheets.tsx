import { useMemo, useRef, useState, type ChangeEvent, type ReactNode } from 'react';
import { FileViewer } from './components/files';
import { Icon, type IconName } from './components/icons';
import { Sheet } from './components/Sheet';
import { cx, haptic, riseStyle } from './components/ui';
import { ActionsContext, type AppActions } from './lib/actions';
import type { Expense, Income, Note, StoredObject, VoiceMode } from './lib/types';
import { DropSheet, FileActionsSheet, UploadSheet } from './views/drive/DriveSheets';
import ExpenseEditor, { type ExpenseMode } from './views/money/ExpenseEditor';
import { IncomeSheet } from './views/money/MoneySheets';
import NoteEditor from './views/notes/NoteEditor';
import VoiceSheet from './views/notes/VoiceSheet';

// Each opened sheet gets a fresh session id, so its state starts clean and it can animate out
// while a new one opens.
interface Session<T> {
  id: number;
  open: boolean;
  data: T;
}

function useSession<T>() {
  const [s, setS] = useState<Session<T> | null>(null);
  const next = useRef(1);
  return {
    s,
    open: (data: T) => setS({ id: next.current++, open: true, data }),
    close: () => setS((cur) => (cur ? { ...cur, open: false } : cur)),
  };
}

const CAPTURE: { id: 'voice' | 'note' | 'receipt' | 'upload'; icon: IconName; label: string; hint: string; tone: string }[] = [
  { id: 'voice', icon: 'mic', label: 'Voice', hint: 'Speak; get a note, tasks or money', tone: 'bg-danger-400/15 text-danger-400' },
  { id: 'note', icon: 'edit', label: 'Text note', hint: 'Markdown, saves as you type', tone: 'bg-signal/15 text-signal' },
  { id: 'receipt', icon: 'camera', label: 'Receipt', hint: 'Snap it; the total is read for you', tone: 'bg-mint-400/15 text-mint-400' },
  { id: 'upload', icon: 'upload', label: 'Upload', hint: 'Files to your Drive', tone: 'bg-[#7FA8FF]/15 text-[#A9CBFF]' },
];

export default function AppSheets({ children, captureOpen, setCaptureOpen }: { children: ReactNode; captureOpen: boolean; setCaptureOpen: (v: boolean) => void }) {
  const note = useSession<{ note: Note | null; seed?: { notebook?: string; body?: string } }>();
  const voice = useSession<{ mode?: VoiceMode }>();
  const income = useSession<Income | null>();
  const expense = useSession<ExpenseMode>();
  const upload = useSession<{ files: File[]; folder: string }>();
  const actions = useSession<StoredObject>();
  const drop = useSession<StoredObject>();
  const [viewer, setViewer] = useState<{ items: StoredObject[]; index: number } | null>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const pickRef = useRef<HTMLInputElement>(null);
  const uploadFolder = useRef('');

  const value = useMemo<AppActions>(
    () => ({
      openCapture: () => setCaptureOpen(true),
      newNote: (seed) => note.open({ note: null, seed }),
      openNote: (n) => note.open({ note: n }),
      recordVoice: () => voice.open({}),
      scanReceipt: () => cameraRef.current?.click(),
      addExpense: () => expense.open({ kind: 'new' }),
      editExpense: (e: Expense) => expense.open({ kind: 'edit', expense: e }),
      recordMoney: () => voice.open({ mode: 'money' }),
      editIncome: (i: Income | null) => income.open(i),
      pickUpload: (folder = '') => {
        uploadFolder.current = folder;
        pickRef.current?.click();
      },
      preview: (obj, siblings) => {
        const items = siblings && siblings.some((o) => o.key === obj.key && o.backend === obj.backend) ? siblings : [obj];
        setViewer({ items, index: Math.max(0, items.findIndex((o) => o.key === obj.key && o.backend === obj.backend)) });
      },
      fileActions: (obj) => actions.open(obj),
      shareDrop: (obj) => drop.open(obj),
    }),
    // The session helpers are stable in behaviour; re-creating them per render is fine.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [setCaptureOpen],
  );

  const onCamera = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) expense.open({ kind: 'receipt', file });
  };
  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = '';
    if (files.length) upload.open({ files, folder: uploadFolder.current });
  };

  const capture = (id: (typeof CAPTURE)[number]['id']) => {
    haptic(8);
    setCaptureOpen(false);
    if (id === 'voice') value.recordVoice();
    if (id === 'note') value.newNote();
    if (id === 'receipt') value.scanReceipt();
    if (id === 'upload') value.pickUpload();
  };

  return (
    <ActionsContext.Provider value={value}>
      {children}

      <input ref={cameraRef} type="file" accept="image/*" capture="environment" className="hidden" onChange={onCamera} aria-label="Receipt photo" />
      <input ref={pickRef} type="file" multiple className="hidden" onChange={onPick} aria-label="Files to upload" />

      <Sheet open={captureOpen} onClose={() => setCaptureOpen(false)} title="Capture">
        <div className="grid grid-cols-2 gap-3 pb-2">
          {CAPTURE.map((c, i) => (
            <button
              key={c.id}
              type="button"
              onClick={() => capture(c.id)}
              style={riseStyle(i + 1)}
              className="press rise flex min-h-[132px] flex-col items-start justify-between rounded-card bg-ink-850 p-4 text-left ring-1 ring-inset ring-ink-700 active:bg-ink-800"
            >
              <span className={cx('grid h-12 w-12 place-items-center rounded-full', c.tone)}>
                <Icon name={c.icon} size={24} />
              </span>
              <span>
                <span className="block font-display text-[17px] font-semibold text-ink-100">{c.label}</span>
                <span className="block text-[13px] leading-snug text-ink-400">{c.hint}</span>
              </span>
            </button>
          ))}
        </div>
      </Sheet>

      {note.s && <NoteEditor key={note.s.id} open={note.s.open} note={note.s.data.note} seed={note.s.data.seed} onClose={note.close} />}
      {voice.s && (
        <VoiceSheet
          key={voice.s.id}
          open={voice.s.open}
          onClose={voice.close}
          initialMode={voice.s.data.mode}
          onOpenNote={(n) => {
            voice.close();
            note.open({ note: n });
          }}
          onEditExpense={(e) => {
            voice.close();
            expense.open({ kind: 'edit', expense: e });
          }}
          onEditIncome={(i) => {
            voice.close();
            income.open(i);
          }}
        />
      )}
      {income.s && <IncomeSheet key={income.s.id} open={income.s.open} income={income.s.data} onClose={income.close} />}
      {expense.s && <ExpenseEditor key={expense.s.id} open={expense.s.open} mode={expense.s.data} onClose={expense.close} />}
      {upload.s && <UploadSheet key={upload.s.id} open={upload.s.open} files={upload.s.data.files} folder={upload.s.data.folder} onClose={upload.close} />}
      {viewer && (
        <FileViewer
          items={viewer.items}
          index={viewer.index}
          onIndex={(index) => setViewer((v) => (v ? { ...v, index } : v))}
          onClose={() => setViewer(null)}
          onActions={(obj) => actions.open(obj)}
        />
      )}
      {actions.s && (
        <FileActionsSheet
          key={actions.s.id}
          open={actions.s.open}
          obj={actions.s.data}
          onClose={actions.close}
          onPreview={() => {
            const obj = actions.s!.data;
            actions.close();
            value.preview(obj, viewer?.items);
          }}
          onShare={() => {
            const obj = actions.s!.data;
            actions.close();
            drop.open(obj);
          }}
        />
      )}
      {drop.s && <DropSheet key={drop.s.id} open={drop.s.open} obj={drop.s.data} onClose={drop.close} />}
    </ActionsContext.Provider>
  );
}
