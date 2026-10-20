import { useState } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Badge, Button, ErrorNote, Spinner, haptic, riseStyle } from '../../components/ui';
import { formatMoney } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { ago, emailMailbox, skipReasons, syncSummary } from '../../lib/digest';
import { errorText, useHub, useHubData } from '../../lib/hub';
import type { InboxSyncResult } from '../../lib/types';

/** "Import from email": syncs every configured mailbox (up to ~50 s on the hub) and shows what was created or skipped. */
export function GmailImport() {
  const { client, notify } = useHub();
  const status = useHubData((c, s) => c.inboxStatus(s), []);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InboxSyncResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const run = async () => {
    haptic(8);
    setBusy(true);
    setError(null);
    try {
      const r = await client.syncGmailReceipts({});
      setResult(r);
      setOpen(true);
      if (r.created.length) announce('expenses');
      notify(syncSummary(r), r.errors.length && !r.created.length ? 'error' : 'ok');
      status.reload();
    } catch (err) {
      setError(errorText(err));
      setOpen(true);
    } finally {
      setBusy(false);
    }
  };

  const configured = status.data?.configured;
  const last = status.data?.last_sync_at;
  const boxes = status.data?.accounts.filter((a) => a.configured).length ?? 0;
  const failing = status.data?.accounts.filter((a) => a.last_error).length ?? 0;
  return (
    <>
      <button
        type="button"
        onClick={() => void run()}
        disabled={busy}
        aria-busy={busy}
        className="press flex min-h-[56px] w-full items-center gap-3 rounded-row bg-ink-900 px-4 py-2 text-left ring-1 ring-inset ring-ink-800 disabled:cursor-wait"
      >
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[#7FA8FF]/15 text-[#A9CBFF]">
          {busy ? <Spinner /> : <Icon name="mail" size={18} />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[15px] text-ink-100">{busy ? 'Reading your receipts…' : 'Import from email'}</span>
          <span className="block truncate text-[12px] text-ink-500">
            {busy
              ? 'This can take up to a minute'
              : configured === false
                ? 'Needs a mailbox app password on the hub'
                : `${boxes > 1 ? `${boxes} mailboxes · ` : ''}${last ? `last import ${ago(last)}` : 'receipts and invoices from the last 7 days'}`}
          </span>
        </span>
        {configured === false && <Badge tone="warn">setup</Badge>}
        {configured && failing > 0 && !busy && <Badge tone="bad">{failing} failing</Badge>}
      </button>

      <Sheet open={open} onClose={() => setOpen(false)} title="Email import" footer={<Button className="w-full" onClick={() => setOpen(false)}>Done</Button>}>
        <div className="space-y-4 pb-2">
          {error && <ErrorNote message={error} />}
          {error && configured === false && (
            <p className="px-1 text-[14px] text-ink-400">
              Create an app password for each mailbox and list the mailboxes in the hub vault (INBOX_ACCOUNTS). See docs/GMAIL.md.
            </p>
          )}
          {result && <SyncResult result={result} />}
          {!result && !!status.data?.accounts.length && (
            <ul className="space-y-1.5">
              {status.data.accounts.map((a) => (
                <li key={a.name} className="flex items-center justify-between gap-2 rounded-row bg-ink-850 px-3 py-2 text-[13px]">
                  <span className="min-w-0 truncate text-ink-200">{a.user}</span>
                  <span className="shrink-0 text-ink-500">{a.last_error ?? (a.configured ? (a.last_sync_at ? ago(a.last_sync_at) : 'never') : 'no password')}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Sheet>
    </>
  );
}

function SyncResult({ result }: { result: InboxSyncResult }) {
  const reasons = skipReasons(result);
  return (
    <>
      <div className="grid grid-cols-3 gap-2">
        {[
          { label: 'Found', value: result.scanned },
          { label: result.dry_run ? 'Would add' : 'Added', value: result.created.length },
          { label: 'Skipped', value: result.skipped.length },
        ].map((s, i) => (
          <div key={s.label} className="rise rounded-row bg-ink-850 px-3 py-2.5 ring-1 ring-inset ring-ink-700" style={riseStyle(i)}>
            <p className="text-[12px] text-ink-400">{s.label}</p>
            <p className="font-display text-[22px] font-semibold leading-tight text-ink-100">{s.value}</p>
          </div>
        ))}
      </div>

      {result.accounts.length > 1 && (
        <ul className="space-y-1.5" aria-label="Mailboxes">
          {result.accounts.map((a) => (
            <li key={a.name} className="flex items-center gap-2 rounded-row bg-ink-850 px-3 py-2 text-[13px] ring-1 ring-inset ring-ink-700">
              <Icon name="mail" size={14} className={a.error ? 'shrink-0 text-danger-400' : 'shrink-0 text-ink-400'} />
              <span className="min-w-0 flex-1 truncate text-ink-200">{a.user}</span>
              <span className={a.error ? 'shrink-0 truncate text-danger-400' : 'shrink-0 text-ink-400'}>
                {a.error ?? `${a.created} added${a.remaining ? `, ${a.remaining} left` : ''}`}
              </span>
            </li>
          ))}
        </ul>
      )}

      {result.created.length > 0 && (
        <section>
          <h3 className="px-1 pb-2 font-display text-[15px] font-semibold text-ink-300">New expenses</h3>
          <ul className="divide-y divide-ink-800 overflow-hidden rounded-row bg-ink-850 ring-1 ring-inset ring-ink-700">
            {result.created.map((e, i) => (
              <li key={e.id ?? `${e.source_ref}-${i}`} className="rise flex min-h-[52px] items-center gap-3 px-3 py-2" style={riseStyle(i)}>
                <Icon name={e.receipt_key ? 'receipt' : 'mail'} size={16} className="shrink-0 text-ink-400" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[14px] text-ink-100">{e.merchant || 'Unknown'}</span>
                  <span className="block truncate text-[12px] text-ink-500">
                    <span className="capitalize">{e.category}</span> · {e.date}
                    {result.accounts.length > 1 && emailMailbox(e.source_ref) ? ` · ${emailMailbox(e.source_ref)}` : ''}
                  </span>
                </span>
                <span className="shrink-0 font-display text-[15px] font-semibold text-ink-100">{formatMoney(e.total, e.currency)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {reasons.length > 0 && (
        <section>
          <h3 className="px-1 pb-2 font-display text-[15px] font-semibold text-ink-300">Skipped</h3>
          <ul className="flex flex-wrap gap-1.5">
            {reasons.map((r) => (
              <li key={r.reason}>
                <Badge tone={r.reason.startsWith('already') ? 'neutral' : 'signal'}>
                  {r.reason} · {r.count}
                </Badge>
              </li>
            ))}
          </ul>
        </section>
      )}

      {result.errors.length > 0 && (
        <section className="space-y-2">
          {result.errors.map((e, i) => (
            <ErrorNote key={`${e.account}-${e.message_id}-${i}`} message={e.account ? `${e.account}: ${e.error}` : e.error} />
          ))}
        </section>
      )}

      {result.remaining > 0 && (
        <p className="rounded-row bg-signal/10 px-4 py-3 text-[14px] text-signal">
          {result.remaining} more email{result.remaining === 1 ? '' : 's'} to read: run the import again to continue.
        </p>
      )}
      {!result.created.length && !result.skipped.length && !result.errors.length && (
        <p className="px-1 text-[14px] text-ink-400">No receipt emails in the last 7 days.</p>
      )}
    </>
  );
}
