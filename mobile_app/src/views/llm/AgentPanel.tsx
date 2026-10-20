import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Badge, Button, Card, EmptyState, ErrorNote, IconButton, SectionTitle, Spinner, cx, inputClass } from '../../components/ui';
import { formatDateTime, formatTime } from '../../lib/format';
import { errorText, useHub, useHubData, useHubEvents } from '../../lib/hub';
import type { AgentTask, AgentTaskStatus, HubEvent } from '../../lib/types';
import ModelPicker, { parseModelChoice } from './ModelPicker';

interface LiveState {
  steps: { ts: string; text: string }[];
  screenshot?: string;
  screenshotAt?: string;
  status?: AgentTaskStatus;
  result?: string | null;
  error?: string | null;
}

const STATUS_TONE: Record<AgentTaskStatus, 'neutral' | 'ok' | 'warn' | 'bad' | 'signal'> = {
  queued: 'neutral',
  running: 'signal',
  completed: 'ok',
  failed: 'bad',
  cancelled: 'warn',
};

// Instinct step payloads (events and StepRecord): {step: int, action?: {action, url?, element?, text?...},
// thought?, result?, error?, url?, routed_via?}. Older/free-form payloads may carry a plain string.
function stepText(data: unknown): string {
  if (typeof data === 'string') return data;
  if (!data || typeof data !== 'object') return String(data);
  const d = data as Record<string, unknown>;
  for (const k of ['text', 'message']) {
    const v = d[k];
    if (typeof v === 'string' && v) return v;
  }
  const parts: string[] = [];
  if (typeof d.step === 'number') parts.push(`#${d.step}`);
  else if (typeof d.step === 'string' && d.step) parts.push(d.step);
  const action = d.action;
  if (action && typeof action === 'object') {
    const a = action as Record<string, unknown>;
    const target = a.url ?? a.selector ?? (a.element !== undefined ? `[${String(a.element)}]` : undefined);
    parts.push([a.action, target, typeof a.text === 'string' ? `"${a.text}"` : undefined].filter(Boolean).join(' '));
  } else if (typeof action === 'string' && action) parts.push(action);
  if (typeof d.thought === 'string' && d.thought) parts.push(`- ${d.thought}`);
  if (typeof d.error === 'string' && d.error) parts.push(`error: ${d.error}`);
  else if (typeof d.result === 'string' && d.result) parts.push(`-> ${d.result.slice(0, 200)}`);
  return parts.length ? parts.join(' ') : JSON.stringify(d);
}

export default function AgentPanel() {
  const { client, notify } = useHub();
  const tasks = useHubData((c, s) => c.agentTasks(s), [], 15_000);
  const models = useHubData((c, s) => c.models(s));
  const [live, setLive] = useState<Record<string, LiveState>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [goal, setGoal] = useState('');
  const [startUrl, setStartUrl] = useState('');
  const [modelChoice, setModelChoice] = useState('');
  const [maxSteps, setMaxSteps] = useState(25);
  const [submitting, setSubmitting] = useState(false);

  useHubEvents(['instinct.task.*'], (e: HubEvent) => {
    const id = typeof e.data.task_id === 'string' ? e.data.task_id : null;
    if (!id) return;
    setLive((prev) => {
      const cur: LiveState = prev[id] ?? { steps: [] };
      const next: LiveState = { ...cur };
      switch (e.type) {
        case 'instinct.task.created':
          next.status = 'running';
          break;
        case 'instinct.task.step':
          next.steps = [...cur.steps, { ts: e.ts, text: stepText(e.data) }].slice(-200);
          next.status = 'running';
          break;
        case 'instinct.task.screenshot':
          if (typeof e.data.image_b64 === 'string') {
            next.screenshot = e.data.image_b64;
            next.screenshotAt = e.ts;
          }
          break;
        case 'instinct.task.completed':
          next.status = 'completed';
          next.result = typeof e.data.result === 'string' ? e.data.result : JSON.stringify(e.data.result ?? null);
          break;
        case 'instinct.task.failed':
          next.status = 'failed';
          next.error = typeof e.data.error === 'string' ? e.data.error : 'failed';
          break;
        default:
          return prev;
      }
      return { ...prev, [id]: next };
    });
    if (e.type === 'instinct.task.created' || e.type === 'instinct.task.completed' || e.type === 'instinct.task.failed') tasks.reload();
  });

  const list = useMemo(() => {
    const items = [...(tasks.data ?? [])];
    items.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
    return items;
  }, [tasks.data]);

  useEffect(() => {
    if (!selected && list.length) setSelected(list[0].id);
  }, [list, selected]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!goal.trim()) return;
    setSubmitting(true);
    try {
      const { model } = parseModelChoice(modelChoice);
      const task = await client.createAgentTask({
        goal: goal.trim(),
        start_url: startUrl.trim() || undefined,
        model: model ?? undefined,
        max_steps: maxSteps,
      });
      tasks.setData((prev) => [task, ...(prev ?? []).filter((t) => t.id !== task.id)]);
      setSelected(task.id);
      setGoal('');
      notify('Task launched', 'ok');
    } catch (err) {
      notify(errorText(err), 'error');
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async (task: AgentTask) => {
    try {
      const updated = await client.cancelAgentTask(task.id);
      tasks.setData((prev) => (prev ?? []).map((t) => (t.id === task.id ? { ...t, ...updated } : t)));
    } catch (err) {
      notify(errorText(err), 'error');
    }
  };

  const current = list.find((t) => t.id === selected) ?? null;
  const currentLive = current ? live[current.id] : undefined;
  const status: AgentTaskStatus | undefined = currentLive?.status ?? current?.status;
  const screenshot = currentLive?.screenshot ?? current?.last_screenshot_b64 ?? undefined;
  const steps = currentLive?.steps.length ? currentLive.steps.map((s) => s) : (current?.steps ?? []).map((step) => ({ ts: '', text: stepText(step) }));

  return (
    <div className="space-y-4">
      <Card className="p-3">
        <form onSubmit={submit} className="space-y-2">
          <label className="block text-sm text-ink-200" htmlFor="agent-goal">
            New Instinct task
          </label>
          <textarea
            id="agent-goal"
            className={cx(inputClass, 'min-h-[72px] py-2')}
            placeholder="e.g. Find the current Groq free-tier limits and summarise them"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
          <input className={inputClass} type="url" inputMode="url" placeholder="Start URL (optional)" value={startUrl} onChange={(e) => setStartUrl(e.target.value)} />
          <div className="flex gap-2">
            <div className="min-w-0 flex-1">
              <ModelPicker models={models.data} value={modelChoice} onChange={setModelChoice} />
            </div>
            <input
              aria-label="Max steps"
              title="Max steps"
              className={cx(inputClass, '!w-20 shrink-0 text-center font-mono')}
              type="number"
              min={1}
              max={200}
              value={maxSteps}
              onChange={(e) => setMaxSteps(Math.max(1, Math.min(200, Number(e.target.value) || 1)))}
            />
          </div>
          <Button type="submit" variant="primary" icon="play" className="w-full" disabled={submitting || !goal.trim()}>
            {submitting ? 'Launching...' : 'Launch task'}
          </Button>
        </form>
      </Card>

      {current && (
        <Card className="overflow-hidden">
          <div className="flex items-center gap-2 border-b border-ink-800 px-3 py-2">
            <p className="min-w-0 flex-1 truncate text-sm text-ink-100">{current.goal}</p>
            {status && <Badge tone={STATUS_TONE[status]}>{status}</Badge>}
            {(status === 'running' || status === 'queued') && <IconButton icon="stop" label="Cancel task" onClick={() => void cancel(current)} />}
          </div>
          <div className="relative aspect-video bg-ink-950">
            {screenshot ? (
              <img src={`data:image/jpeg;base64,${screenshot}`} alt="Latest browser screenshot" className="h-full w-full object-contain" />
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-ink-500">
                {status === 'running' || status === 'queued' ? 'Waiting for the first screenshot...' : 'No screenshot'}
              </div>
            )}
            {currentLive?.screenshotAt && (
              <span className="absolute bottom-2 right-2 rounded bg-ink-950/80 px-1.5 py-0.5 font-mono text-[10px] text-ink-300">{formatTime(currentLive.screenshotAt)}</span>
            )}
          </div>
          <ol className="scroll-thin max-h-56 space-y-1 overflow-y-auto px-3 py-2 font-mono text-[12px]">
            {steps.length === 0 && <li className="text-ink-500">No steps yet.</li>}
            {steps.map((s, i) => (
              <li key={i} className="flex gap-2 text-ink-300">
                <span className="w-6 shrink-0 text-right text-ink-500">{i + 1}</span>
                <span className="min-w-0 break-words">{s.text}</span>
              </li>
            ))}
          </ol>
          {(currentLive?.result ?? current.result) && (
            <div className="border-t border-ink-800 px-3 py-2 text-sm text-ink-100">
              <p className="mb-1 text-[11px] uppercase tracking-wider text-signal">Result</p>
              <p className="whitespace-pre-wrap">{currentLive?.result ?? current.result}</p>
            </div>
          )}
          {(currentLive?.error ?? current.error) && (
            <div className="p-3">
              <ErrorNote message={String(currentLive?.error ?? current.error)} />
            </div>
          )}
        </Card>
      )}

      <div>
        <SectionTitle action={tasks.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh tasks" onClick={tasks.reload} />}>Tasks</SectionTitle>
        {tasks.error && <ErrorNote message={tasks.error} onRetry={tasks.reload} />}
        {!tasks.loading && !tasks.error && list.length === 0 && <EmptyState icon="agent" title="No agent tasks yet" />}
        <ul className="space-y-1.5">
          {list.map((t) => {
            const st = live[t.id]?.status ?? t.status;
            return (
              <li key={t.id}>
                <button
                  type="button"
                  onClick={() => setSelected(t.id)}
                  className={cx(
                    'flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left transition-colors',
                    t.id === selected ? 'border-signal/40 bg-ink-850' : 'border-ink-800 bg-ink-900 hover:border-ink-700',
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-ink-100">{t.goal}</span>
                    <span className="font-mono text-[11px] text-ink-500">{formatDateTime(t.created_at)}</span>
                  </span>
                  <Badge tone={STATUS_TONE[st] ?? 'neutral'}>{st}</Badge>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
