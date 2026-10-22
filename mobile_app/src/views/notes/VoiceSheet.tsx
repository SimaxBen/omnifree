import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Icon } from '../../components/icons';
import { RecordButton, canRecord, formatClock, useRecorder } from '../../components/Recorder';
import { Sheet, useConfirm } from '../../components/Sheet';
import { Badge, Button, Chip, ErrorNote, Segmented, cx, riseStyle } from '../../components/ui';
import { formatMoney, isoDay } from '../../lib/apps';
import { plainText } from '../../lib/markdown';
import { announce } from '../../lib/bus';
import { isQueuedVoice } from '../../lib/offline/client';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { loadPendingMeeting } from '../../lib/meeting';
import { voiceMoneySummary } from '../../lib/money';
import { readPref, writePref } from '../../lib/settings';
import { eventSlot, slotLabel } from '../../lib/tasks';
import type { Expense, GCalEvent, Income, Note, Task, VoiceMode, VoiceMoneyKind, VoiceNoteResult } from '../../lib/types';
import { CATEGORY_COLOR } from '../money/ExpenseEditor';
import { INCOME_LABEL } from '../money/MoneySheets';
import MeetingPanel from './MeetingPanel';
import { TaskRow } from './TaskRow';

// 'both' behaves like 'auto' on the hub (a note plus any task), so the phone shows four clear choices.
const MODES: { value: VoiceMode; label: string }[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'note', label: 'Note' },
  { value: 'tasks', label: 'Tasks' },
  { value: 'money', label: 'Money' },
];

const MODE_HINT: Record<VoiceMode, string> = {
  auto: 'A note, plus tasks, appointments and money when you mention them.',
  note: 'A cleaned-up note only.',
  tasks: 'Tasks only (a note is kept if no task is heard).',
  both: 'A note and every task in it.',
  money: 'Say what you spent or received: "45 dh taxi, 30 dh coffee".',
};

const MONEY_KINDS: { value: VoiceMoneyKind; label: string }[] = [
  { value: 'auto', label: 'Spent or received' },
  { value: 'expense', label: 'Spent' },
  { value: 'income', label: 'Received' },
];

function storedMode(): VoiceMode {
  const m = readPref<VoiceMode>('voice.mode', 'auto');
  return m === 'both' || !MODES.some((x) => x.value === m) ? 'auto' : m;
}

/** Record, send to POST /notes/voice (POST /money/voice in money mode), show what the hub made of it. */
export default function VoiceSheet({
  open,
  onClose,
  onOpenNote,
  initialMode,
  onEditExpense,
  onEditIncome,
}: {
  open: boolean;
  onClose: () => void;
  onOpenNote: (n: Note) => void;
  /** Opened from a money screen: start in that mode without changing the saved choice. */
  initialMode?: VoiceMode;
  onEditExpense?: (e: Expense) => void;
  onEditIncome?: (i: Income) => void;
}) {
  const { client, notify } = useHub();
  const rec = useRecorder();
  const [mode, setMode] = useState<VoiceMode>(() => initialMode ?? storedMode());
  const [moneyKind, setMoneyKind] = useState<VoiceMoneyKind>('auto');
  const [notebook, setNotebook] = useState<string>(() => readPref('voice.notebook', 'Inbox'));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<VoiceNoteResult | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [events, setEvents] = useState<GCalEvent[]>([]);
  const [removing, setRemoving] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const [meeting, setMeeting] = useState(false);
  const [meetingRecording, setMeetingRecording] = useState(false);
  // A meeting whose note was never written (sheet closed, app reloaded): the meeting panel offers to finish it.
  const [pendingMeeting] = useState(() => loadPendingMeeting() !== null);
  const confirm = useConfirm();
  const abort = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const touched = useRef(false);
  const books = useHubData((c, s) => c.notes({ limit: 1 }, s), []);

  useEffect(() => {
    if (!initialMode || touched.current) writePref('voice.mode', mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);
  useEffect(() => {
    writePref('voice.notebook', notebook);
  }, [notebook]);
  useEffect(
    () => () => {
      abort.current?.abort();
    },
    [],
  );

  const send = async (blob: Blob, filename: string) => {
    setBusy(true);
    setError(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    try {
      let res: VoiceNoteResult;
      if (mode === 'money') {
        const m = await client.voiceMoney(blob, { kind: moneyKind, filename }, ctrl.signal);
        res = isQueuedVoice(m) ? (m as unknown as VoiceNoteResult) : { transcript: m.transcript, audio_key: m.audio_key, note: null, tasks: [], expenses: m.expenses, incomes: m.incomes };
      } else {
        res = await client.voiceNote(blob, { mode, notebook, filename }, ctrl.signal);
      }
      if (isQueuedVoice(res)) {
        // Offline: the recording is kept on the phone and uploaded when the hub answers again.
        notify('Saved offline. It will be transcribed when you are back online.', 'ok');
        rec.reset();
        onClose();
        return;
      }
      setResult(res);
      setTasks(res.tasks);
      setEvents(res.events ?? []);
      announce('notes', 'tasks');
      if (res.expenses?.length || res.incomes?.length) announce('expenses', 'money');
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        const msg = errorText(err);
        setError(/422/.test(msg) || /speech|transcript/i.test(msg) ? `${msg}. Try again a little closer to the microphone.` : msg);
      }
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  // Send as soon as a recording stops.
  const lastClip = useRef<Blob | null>(null);
  useEffect(() => {
    if (rec.clip && rec.clip.blob !== lastClip.current && !result) {
      lastClip.current = rec.clip.blob;
      void send(rec.clip.blob, rec.clip.name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rec.clip]);

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = '';
    if (f) void send(f, f.name);
  };

  const again = () => {
    setResult(null);
    setTasks([]);
    setEvents([]);
    setError(null);
    rec.reset();
  };

  const expenses = result?.expenses ?? [];
  const incomes = result?.incomes ?? [];
  const undoMoney = async () => {
    setUndoing(true);
    try {
      await Promise.all([...expenses.map((e) => client.deleteExpense(e.id)), ...incomes.map((i) => client.deleteIncome(i.id))]);
      announce('expenses', 'money');
      notify(`Removed ${voiceMoneySummary(expenses, incomes)}`, 'ok');
      if (!result?.note && !tasks.length) again();
      else setResult((r) => r && { ...r, expenses: [], incomes: [] });
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setUndoing(false);
    }
  };

  const undoEvent = async (ev: GCalEvent) => {
    setRemoving(ev.id);
    try {
      await client.deleteCalendarEvent(ev.id);
      setEvents((es) => es.filter((e) => e.id !== ev.id));
      notify(`Removed ${ev.title} from the calendar`, 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setRemoving(null);
    }
  };

  // A stray swipe, backdrop tap or Back during a meeting must not end it unasked; once confirmed, the recording
  // stops and MeetingPanel finishes the last part and the note in the background.
  const close = async () => {
    if (meeting && meetingRecording) {
      const ok = await confirm({
        title: 'End the meeting?',
        body: 'Recording stops. What was recorded is still transcribed and the note is written in the background.',
        confirmLabel: 'End meeting',
      });
      if (!ok) return;
    }
    onClose();
  };

  const notebooks = Array.from(new Set(['Inbox', ...(books.data?.notebooks.map((b) => b.name) ?? [])]));

  return (
    <Sheet open={open} onClose={() => void close()} title={meeting ? 'Meeting' : result ? 'Captured' : mode === 'money' ? 'Voice money' : 'Voice note'}>
      {meeting ? (
        <MeetingPanel onOpenNote={onOpenNote} onClose={onClose} onRecordingChange={setMeetingRecording} />
      ) : !result ? (
        <div className="flex flex-col items-center gap-5 pb-2">
          <div className="w-full space-y-2">
            <Segmented
              value={mode}
              onChange={(m) => {
                touched.current = true;
                setMode(m);
              }}
              options={MODES}
            />
            <p className="px-1 text-center text-[13px] text-ink-400">{MODE_HINT[mode]}</p>
          </div>

          <div className="relative flex flex-col items-center">
            {busy ? (
              <div className="grid h-[168px] place-items-center text-center">
                <div className="space-y-3">
                  <div className="dot-wave font-display text-[32px] leading-none text-signal">
                    <span>.</span>
                    <span>.</span>
                    <span>.</span>
                  </div>
                  <p className="text-[15px] text-ink-200">{mode === 'money' ? 'Transcribing and counting' : 'Transcribing and sorting'}</p>
                  <p className="text-[13px] text-ink-500">Usually a few seconds</p>
                </div>
              </div>
            ) : canRecord() ? (
              <RecordButton recording={rec.recording} level={rec.level} onPress={() => (rec.recording ? rec.stop() : void rec.start())} />
            ) : (
              <p className="py-10 text-center text-[14px] text-ink-400">This browser cannot record. Pick an audio file instead.</p>
            )}
            {!busy && (
              <p className="mt-1 font-display text-[28px] font-semibold tabular-nums text-ink-100" aria-live="polite">
                {rec.recording ? formatClock(rec.elapsed) : 'Tap to record'}
              </p>
            )}
          </div>

          {!rec.recording && !busy && mode === 'money' && (
            <div className="no-scrollbar -mx-5 flex w-[calc(100%+40px)] gap-2 overflow-x-auto px-5">
              {MONEY_KINDS.map((k) => (
                <Chip key={k.value} active={k.value === moneyKind} onClick={() => setMoneyKind(k.value)}>
                  {k.label}
                </Chip>
              ))}
            </div>
          )}

          {!rec.recording && !busy && mode !== 'money' && (
            <div className="no-scrollbar -mx-5 flex w-[calc(100%+40px)] gap-2 overflow-x-auto px-5">
              {notebooks.map((n) => (
                <Chip key={n} active={n === notebook} onClick={() => setNotebook(n)}>
                  <Icon name="book" size={14} />
                  {n}
                </Chip>
              ))}
            </div>
          )}

          {(rec.error || error) && (
            <div className="w-full space-y-2">
              <ErrorNote message={(rec.error || error) as string} />
              {rec.clip && error && (
                <Button className="w-full" icon="refresh" onClick={() => void send(rec.clip!.blob, rec.clip!.name)}>
                  Send the recording again
                </Button>
              )}
            </div>
          )}

          {!rec.recording && !busy && (
            <div className="flex flex-wrap items-center justify-center gap-x-5">
              <button type="button" onClick={() => fileRef.current?.click()} className="press min-h-[44px] text-[14px] text-ink-400 underline underline-offset-4">
                Use an audio file instead
              </button>
              {mode !== 'money' && (
                <button type="button" onClick={() => setMeeting(true)} className={cx('press flex min-h-[44px] items-center gap-1.5 text-[14px] font-semibold', pendingMeeting ? 'text-amber-400' : 'text-signal')}>
                  <Icon name="wave" size={16} />
                  {pendingMeeting ? 'Finish the unfinished meeting' : 'Record a meeting'}
                </button>
              )}
            </div>
          )}
          <input ref={fileRef} type="file" accept="audio/*,video/webm,video/mp4" className="hidden" onChange={onFile} aria-label="Audio file" />
        </div>
      ) : (
        <div className="space-y-4 pb-2">
          {rec.clip && <audio src={rec.clip.url} controls className="w-full" />}

          {result.note && (
            <button
              type="button"
              onClick={() => onOpenNote(result.note as Note)}
              className="press rise block w-full rounded-card bg-ink-850 p-4 text-left ring-1 ring-inset ring-ink-700"
            >
              <span className="flex items-center gap-2 text-[13px] text-ink-400">
                <Icon name="note" size={14} />
                Note in {result.note.notebook}
              </span>
              <span className="mt-1 block font-display text-[19px] font-semibold text-ink-100">{result.note.title}</span>
              <span className="mt-1 line-clamp-3 block text-[14px] text-ink-300">{plainText(result.note.body, 220)}</span>
              {result.note.tags.length > 0 && <span className="mt-2 block text-[13px] text-signal">{result.note.tags.map((t) => `#${t}`).join(' ')}</span>}
            </button>
          )}

          {(expenses.length > 0 || incomes.length > 0) && (
            <div className="space-y-1">
              <div className="flex min-h-[44px] items-center justify-between gap-2 px-1">
                <h3 className="font-display text-[16px] font-semibold text-ink-100">Recorded {voiceMoneySummary(expenses, incomes)}</h3>
                <button
                  type="button"
                  onClick={() => void undoMoney()}
                  disabled={undoing}
                  className="press flex min-h-[44px] shrink-0 items-center gap-1 text-[14px] font-semibold text-signal disabled:opacity-40"
                >
                  <Icon name="undo" size={15} />
                  {undoing ? 'Undoing…' : 'Undo'}
                </button>
              </div>
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
                {expenses.map((e, i) => (
                  <button
                    key={e.id}
                    type="button"
                    onClick={() => onEditExpense?.(e)}
                    className="press rise flex min-h-[60px] w-full items-center gap-3 px-3 py-2 text-left"
                    style={riseStyle(i + 1)}
                  >
                    <span
                      className="grid h-9 w-9 shrink-0 place-items-center rounded-full"
                      style={{ background: `${CATEGORY_COLOR[e.category] ?? '#8A8FBF'}22`, color: CATEGORY_COLOR[e.category] ?? '#8A8FBF' }}
                    >
                      <Icon name="wallet" size={16} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[15px] text-ink-100">{e.merchant || 'Expense'}</span>
                      <span className="block truncate text-[12px] capitalize text-ink-500">
                        {e.category}, {e.date}
                      </span>
                    </span>
                    <span className="shrink-0 font-display text-[16px] font-semibold text-ink-100">-{formatMoney(e.total, e.currency)}</span>
                    <Icon name="edit" size={15} className="shrink-0 text-ink-500" />
                  </button>
                ))}
                {incomes.map((inc, i) => (
                  <button
                    key={inc.id}
                    type="button"
                    onClick={() => onEditIncome?.(inc)}
                    className="press rise flex min-h-[60px] w-full items-center gap-3 px-3 py-2 text-left"
                    style={riseStyle(expenses.length + i + 1)}
                  >
                    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-mint-400/15 text-mint-400">
                      <Icon name="cash" size={16} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[15px] text-ink-100">{inc.source || INCOME_LABEL[inc.category]}</span>
                      <span className="block truncate text-[12px] text-ink-500">
                        {INCOME_LABEL[inc.category]}, {inc.date}
                      </span>
                    </span>
                    <span className="shrink-0 font-display text-[16px] font-semibold text-mint-400">+{formatMoney(inc.amount, inc.currency)}</span>
                    <Icon name="edit" size={15} className="shrink-0 text-ink-500" />
                  </button>
                ))}
              </div>
              <p className="px-1 pt-1 text-[12px] text-ink-500">Tap a line to fix it.</p>
            </div>
          )}

          {mode === 'money' && !expenses.length && !incomes.length && (
            <div className="rise rounded-card bg-ink-850 p-4 ring-1 ring-inset ring-ink-700">
              <Badge tone="warn">No amount heard</Badge>
              <p className="pt-2 text-[14px] text-ink-300">Say the amount and what it was for, for example "45 dirhams taxi".</p>
            </div>
          )}

          {tasks.length > 0 && (
            <div className="space-y-1">
              <h3 className="px-1 font-display text-[16px] font-semibold text-ink-100">
                {tasks.length} task{tasks.length === 1 ? '' : 's'} added
              </h3>
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
                {tasks.map((t, i) => (
                  <div key={t.id} className="rise" style={riseStyle(i + 1)}>
                    <TaskRow task={t} onChange={(nt) => setTasks((ts) => ts.map((x) => (x.id === nt.id ? nt : x)))} compact />
                  </div>
                ))}
              </div>
            </div>
          )}

          {events.length > 0 && (
            <div className="space-y-1">
              <h3 className="px-1 font-display text-[16px] font-semibold text-ink-100">
                {events.length} event{events.length === 1 ? '' : 's'} in your calendar
              </h3>
              <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-850">
                {events.map((ev, i) => (
                  <div key={ev.id} className="rise flex min-h-[60px] items-center gap-3 py-2 pl-3 pr-1" style={riseStyle(tasks.length + i + 1)}>
                    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-signal/15 text-signal">
                      <Icon name="calendar" size={16} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[15px] text-ink-100">{ev.title}</span>
                      <span className="block truncate text-[12px] text-ink-500">
                        {slotLabel(eventSlot(ev), isoDay())}
                        {ev.location && <> · {ev.location}</>}
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() => void undoEvent(ev)}
                      disabled={removing === ev.id}
                      aria-label={`Undo ${ev.title}`}
                      className="press flex min-h-[44px] shrink-0 items-center gap-1 px-2 text-[14px] font-semibold text-signal disabled:opacity-40"
                    >
                      <Icon name="undo" size={15} />
                      {removing === ev.id ? 'Undoing…' : 'Undo'}
                    </button>
                  </div>
                ))}
              </div>
              <p className="px-1 pt-1 text-[12px] text-ink-500">Appointments go to the OmniFree Google Calendar; to-dos stay tasks.</p>
            </div>
          )}

          <div>
            <button type="button" onClick={() => setShowTranscript((v) => !v)} className="press flex min-h-[44px] items-center gap-1 px-1 text-[14px] text-ink-400">
              <Icon name={showTranscript ? 'up' : 'down'} size={16} />
              {showTranscript ? 'Hide' : 'Show'} what you said
            </button>
            {showTranscript && <p className="screen-in-up whitespace-pre-wrap rounded-row bg-ink-950 p-3 text-[14px] text-ink-300">{result.transcript}</p>}
          </div>

          <div className="flex gap-2">
            <Button className="flex-1" icon="mic" onClick={again}>
              Record another
            </Button>
            <Button variant="primary" className="flex-1" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      )}
    </Sheet>
  );
}
