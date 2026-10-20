import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Icon } from '../components/icons';
import { Badge, Button, Card, EmptyState, ErrorNote, IconButton, SectionTitle, Spinner, StatusDot, Toggle, cx, inputClass } from '../components/ui';
import { SCHEDULE_PRESETS, checkJsonBody, describeCron, isValidCron } from '../lib/cron';
import { gmailSyncTemplate, scheduleHeaders } from '../lib/digest';
import { formatDateTime } from '../lib/format';
import { errorText, useHub, useHubData } from '../lib/hub';
import type { CronJob, CronJobCreate, CronProvider } from '../lib/types';

const METHODS = ['POST', 'GET', 'PUT', 'PATCH', 'DELETE'];

function providerTone(p: CronProvider): 'ok' | 'warn' | 'bad' | 'off' {
  if (!p.enabled) return 'off';
  if (p.healthy === false) return 'bad';
  if (p.healthy == null) return 'warn';
  return 'ok';
}

const jobKey = (j: CronJob) => `${j.provider}/${j.id}`;

function CreateForm({ providers, onCreated, onCancel }: { providers: CronProvider[]; onCreated: (job: CronJob) => void; onCancel: () => void }) {
  const { client, settings } = useHub();
  const usable = providers.filter((p) => p.enabled);
  const [provider, setProvider] = useState(usable[0]?.name ?? '');
  const [name, setName] = useState('');
  const [preset, setPreset] = useState(SCHEDULE_PRESETS[1].id);
  const [custom, setCustom] = useState('*/30 * * * *');
  const [target, setTarget] = useState('');
  const [method, setMethod] = useState('POST');
  const [body, setBody] = useState('');
  const [withToken, setWithToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const firstUsable = usable[0]?.name ?? '';
  useEffect(() => {
    if (!provider && firstUsable) setProvider(firstUsable);
  }, [provider, firstUsable]);

  const schedule = preset === 'custom' ? custom.trim() : (SCHEDULE_PRESETS.find((p) => p.id === preset)?.schedule ?? '');
  const scheduleOk = isValidCron(schedule);
  const reading = scheduleOk ? describeCron(schedule) : null;
  const bodyCheck = checkJsonBody(body);
  const canSubmit = !busy && !!provider && !!name.trim() && !!target.trim() && scheduleOk && !bodyCheck.error;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const job: CronJobCreate = { provider, name: name.trim(), schedule, target: target.trim(), method };
    if (bodyCheck.body) job.body = bodyCheck.body;
    const headers = scheduleHeaders({ body: bodyCheck.body, token: settings.token, withToken });
    if (headers) job.headers = headers;
    try {
      onCreated(await client.createCronJob(job));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const useGmailTemplate = () => {
    const t = gmailSyncTemplate(settings.hubUrl);
    setName(t.name);
    setPreset(t.preset);
    setTarget(t.target);
    setMethod(t.method);
    setBody(t.body);
    setWithToken(true);
  };

  return (
    <Card className="p-3">
      <form className="space-y-3" onSubmit={(e) => void submit(e)}>
        <button
          type="button"
          onClick={useGmailTemplate}
          className="press flex min-h-[44px] w-full items-center gap-2 rounded-row bg-ink-850 px-3 text-left text-[14px] text-ink-200 ring-1 ring-inset ring-ink-700"
        >
          <Icon name="mail" size={16} className="text-[#A9CBFF]" />
          <span className="min-w-0 flex-1 truncate">Template: import email receipts daily</span>
          <Icon name="right" size={16} className="text-ink-500" />
        </button>
        <div className="grid grid-cols-2 gap-2">
          <label className="block space-y-1 text-xs text-ink-400">
            Provider
            <select className={inputClass} value={provider} onChange={(e) => setProvider(e.target.value)} disabled={usable.length === 0}>
              {usable.length === 0 && <option value="">no enabled provider</option>}
              {usable.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1 text-xs text-ink-400">
            Method
            <select className={inputClass} value={method} onChange={(e) => setMethod(e.target.value)}>
              {METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="block space-y-1 text-xs text-ink-400">
          Name
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} placeholder="nightly-backup" />
        </label>

        <div className="space-y-1.5">
          <p className="text-xs text-ink-400">Schedule</p>
          <div className="flex flex-wrap gap-1.5">
            {[...SCHEDULE_PRESETS, { id: 'custom', label: 'Custom', schedule: '' }].map((p) => (
              <button
                key={p.id}
                type="button"
                aria-pressed={preset === p.id}
                onClick={() => setPreset(p.id)}
                className={cx(
                  'min-h-[36px] rounded-full border px-3 text-xs transition-colors',
                  preset === p.id ? 'border-signal/50 bg-signal/10 text-signal' : 'border-ink-700 text-ink-400 hover:text-ink-200',
                )}
              >
                {p.label}
              </button>
            ))}
          </div>
          {preset === 'custom' && (
            <input
              aria-label="Cron expression"
              className={cx(inputClass, 'font-mono', !scheduleOk && 'border-danger-400/60')}
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder="min hour day month weekday"
              spellCheck={false}
            />
          )}
          <p className={cx('px-1 text-[11px]', scheduleOk ? 'text-ink-500' : 'text-danger-400')}>
            <span className="font-mono text-ink-300">{schedule || '-'}</span>
            {' · '}
            {scheduleOk ? (reading ?? 'custom schedule') : 'needs 5 fields: minute hour day-of-month month day-of-week'} (UTC)
          </p>
        </div>

        <label className="block space-y-1 text-xs text-ink-400">
          Target
          <input
            className={cx(inputClass, 'font-mono')}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="https://example.com/hook"
            inputMode="url"
            spellCheck={false}
          />
        </label>
        <label className="block space-y-1 text-xs text-ink-400">
          JSON body (optional)
          <textarea
            className={cx(inputClass, 'min-h-[64px] py-2 font-mono', bodyCheck.error && 'border-danger-400/60')}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder='{"source": "omnifree"}'
            spellCheck={false}
          />
          {bodyCheck.error && <span className="block text-[11px] text-danger-400">{bodyCheck.error}</span>}
        </label>

        <div className="flex items-center justify-between gap-3 px-1">
          <span className="min-w-0 text-xs text-ink-400">
            <span className="block text-ink-200">Send my hub token</span>
            Adds the Authorization header, for targets on this hub
          </span>
          <Toggle checked={withToken} onChange={setWithToken} label="Send my hub token" />
        </div>

        {error && <ErrorNote message={error} />}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" icon="plus" disabled={!canSubmit}>
            {busy ? 'Creating...' : 'Create schedule'}
          </Button>
        </div>
      </form>
    </Card>
  );
}

export default function SchedulesView() {
  const { client, notify } = useHub();
  const [filter, setFilter] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const providers = useHubData((c, s) => c.cronProviders(s), [], 120_000);
  const jobs = useHubData((c, s) => c.cronJobs(filter || undefined, s), [filter], 60_000);

  const rows = useMemo(
    () => [...(jobs.data?.jobs ?? [])].sort((a, b) => a.provider.localeCompare(b.provider) || a.name.localeCompare(b.name)),
    [jobs.data],
  );
  const errors = Object.entries(jobs.data?.errors ?? {});

  const runNow = async (job: CronJob) => {
    setBusyKey(jobKey(job));
    try {
      await client.triggerCronJob(job.provider, job.id);
      notify(`Triggered ${job.name} on ${job.provider}`, 'ok');
      window.setTimeout(jobs.reload, 3000);
    } catch (err) {
      notify(`${job.name}: ${errorText(err)}`, 'error');
    } finally {
      setBusyKey(null);
    }
  };

  const remove = async (job: CronJob) => {
    setConfirmKey(null);
    setBusyKey(jobKey(job));
    try {
      await client.deleteCronJob(job.provider, job.id);
      jobs.setData((prev) => prev && { ...prev, jobs: prev.jobs.filter((j) => jobKey(j) !== jobKey(job)) });
      notify(`Deleted ${job.name}`, 'ok');
    } catch (err) {
      notify(`${job.name}: ${errorText(err)}`, 'error');
    } finally {
      setBusyKey(null);
    }
  };

  const created = (job: CronJob) => {
    setShowForm(false);
    notify(`Scheduled ${job.name} on ${job.provider}`, 'ok');
    if (!filter || filter === job.provider) jobs.setData((prev) => prev && { ...prev, jobs: [...prev.jobs.filter((j) => jobKey(j) !== jobKey(job)), job] });
    jobs.reload();
  };

  const providerList = providers.data ?? [];

  return (
    <div className="space-y-4">
      <div className="scroll-thin -mx-4 flex gap-1.5 overflow-x-auto px-4">
        {[{ name: '', enabled: true } as CronProvider, ...providerList].map((p) => (
          <button
            key={p.name || 'all'}
            type="button"
            onClick={() => setFilter(p.name)}
            className={cx(
              'flex min-h-[36px] shrink-0 items-center gap-1.5 rounded-full border px-3 font-mono text-xs transition-colors',
              p.name === filter ? 'border-signal/50 bg-signal/10 text-signal' : 'border-ink-700 text-ink-400 hover:text-ink-200',
            )}
            title={p.description ?? undefined}
          >
            {p.name && <StatusDot tone={providerTone(p)} />}
            {p.name || 'all'}
          </button>
        ))}
        {providers.loading && <Spinner className="m-2.5 shrink-0" />}
      </div>
      {providers.error && <ErrorNote message={`Cron providers: ${providers.error}`} onRetry={providers.reload} />}

      {showForm ? (
        <CreateForm providers={providerList} onCreated={created} onCancel={() => setShowForm(false)} />
      ) : (
        <Button variant="primary" icon="plus" className="w-full" onClick={() => setShowForm(true)} disabled={!providerList.some((p) => p.enabled)}>
          New schedule
        </Button>
      )}

      <div>
        <SectionTitle action={jobs.loading ? <Spinner /> : <IconButton icon="refresh" label="Refresh schedules" onClick={jobs.reload} />}>
          Schedules {rows.length > 0 && <span className="text-ink-500">({rows.length})</span>}
        </SectionTitle>
        {jobs.error && <ErrorNote message={jobs.error} onRetry={jobs.reload} />}
        {errors.length > 0 && <p className="mb-2 text-xs text-amber-400">Unreachable: {errors.map(([k, v]) => `${k} (${v})`).join(', ')}</p>}
        {!jobs.loading && !jobs.error && rows.length === 0 && (
          <EmptyState icon="clock" title="No schedules yet" hint="Call a URL on a cron schedule through QStash or GitHub Actions. Times are UTC." />
        )}
        <ul className="space-y-2">
          {rows.map((j) => {
            const k = jobKey(j);
            const reading = describeCron(j.schedule);
            return (
              <li key={k} className={cx('rounded-xl border border-ink-700/70 bg-ink-900/90', !j.enabled && 'opacity-60')}>
                <div className="flex items-start gap-2 p-3 pb-2">
                  <span className="pt-1.5">
                    <StatusDot tone={j.enabled ? 'ok' : 'off'} />
                  </span>
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <p className="flex items-center gap-1.5">
                      <span className="truncate text-sm text-ink-100">{j.name}</span>
                      <Badge>{j.provider}</Badge>
                    </p>
                    <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-ink-300">
                      <Icon name="clock" size={12} className="text-ink-500" />
                      {reading && <span>{reading}</span>}
                      <span className="font-mono text-ink-500">{j.schedule}</span>
                    </p>
                    <p className="truncate font-mono text-[11px] text-ink-400" title={j.target}>
                      <span className="text-ink-500">{j.method || 'POST'}</span> {j.target}
                    </p>
                    <p className="text-[11px] text-ink-500">
                      last {formatDateTime(j.last_run_at)} · next {formatDateTime(j.next_run_at)}
                    </p>
                  </div>
                  {busyKey === k ? (
                    <Spinner className="m-3" />
                  ) : (
                    <span className="flex shrink-0">
                      <IconButton icon="play" label={`Run ${j.name} now`} onClick={() => void runNow(j)} className="h-9 w-9" />
                      <IconButton
                        icon="trash"
                        label={`Delete ${j.name}`}
                        onClick={() => setConfirmKey(confirmKey === k ? null : k)}
                        className={cx('h-9 w-9 hover:text-danger-400', confirmKey === k && 'text-danger-400')}
                      />
                    </span>
                  )}
                </div>
                {confirmKey === k && (
                  <div className="flex items-center gap-2 border-t border-ink-800 px-3 py-2">
                    <p className="min-w-0 flex-1 text-xs text-danger-400">
                      Delete <span className="font-mono">{j.name}</span> from {j.provider}?
                    </p>
                    <Button variant="ghost" className="min-h-[34px]" onClick={() => setConfirmKey(null)}>
                      Cancel
                    </Button>
                    <Button variant="danger" className="min-h-[34px]" onClick={() => void remove(j)}>
                      Delete
                    </Button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
