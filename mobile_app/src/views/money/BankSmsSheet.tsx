import { useState, type ReactNode } from 'react';
import { Icon } from '../../components/icons';
import { Sheet } from '../../components/Sheet';
import { Badge, Button, ErrorNote, Field, IconButton, Segmented, SkeletonList, cx, inputClass, riseStyle } from '../../components/ui';
import { formatMoney } from '../../lib/apps';
import { announce } from '../../lib/bus';
import { copyText } from '../../lib/dom';
import { errorText, useHub, useHubData } from '../../lib/hub';
import { SMS_FORWARDER_TEMPLATE, maskSecret, smsOutcome, smsWebhookUrl } from '../../lib/money';
import type { SmsResult } from '../../lib/types';

const BANK_SENDERS = ['CIH BANK', 'Attijari', 'BMCE / BANK OF AFRICA', 'CHAABI / BCP', 'CAM', 'ALBARID', 'CDM', 'SAHAM', 'CFG', 'BARIDCASH'];

const BADGE_TONE = { ok: 'ok', warn: 'warn', bad: 'bad', neutral: 'neutral' } as const;

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-signal/15 font-display text-[14px] font-semibold text-signal">{n}</span>
      <div className="min-w-0 flex-1 space-y-2 pt-0.5">
        <p className="text-[15px] text-ink-100">{title}</p>
        {children}
      </div>
    </li>
  );
}

function CopyRow({ value, shown, label }: { value: string; shown: string; label: string }) {
  const { notify } = useHub();
  return (
    <div className="flex items-center gap-1 rounded-row bg-ink-950 py-1 pl-3 pr-1 ring-1 ring-inset ring-ink-800">
      <code className="min-w-0 flex-1 break-all font-mono text-[12px] leading-snug text-ink-200">{shown}</code>
      <IconButton
        icon="copy"
        label={`Copy ${label}`}
        onClick={async () => {
          const ok = await copyText(value);
          notify(ok ? `${label} copied` : 'Copy failed: select the text instead', ok ? 'ok' : 'error');
        }}
      />
    </div>
  );
}

/** Bank SMS alerts: forwarder app setup, a dry-run test box and the last SMS received. */
export function BankSmsSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { client, settings } = useHub();
  const setup = useHubData((c, s) => c.smsSetup(s), []);
  const log = useHubData((c, s) => c.smsLog(20, s), []);
  const [mode, setMode] = useState<'query' | 'header'>('query');
  const [reveal, setReveal] = useState(false);
  const [sample, setSample] = useState('');
  const [sender, setSender] = useState('');
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<SmsResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const s = setup.data;
  const base = s?.public_base_url || settings.hubUrl;
  const token = s?.token ?? '';
  const urlReal = s ? smsWebhookUrl(base, s.path, mode === 'query' ? token : null) : '';
  const urlShown = s ? `${smsWebhookUrl(base, s.path)}${mode === 'query' && token ? `?token=${reveal ? token : maskSecret(token)}` : ''}` : '';

  const test = async () => {
    if (!sample.trim()) return;
    setTesting(true);
    setTestError(null);
    try {
      setResult(await client.testSms(sample.trim(), sender.trim()));
    } catch (err) {
      setTestError(errorText(err));
    } finally {
      setTesting(false);
    }
  };

  const p = result?.parsed;
  const outcome = result ? smsOutcome({ status: result.status, kind: p?.kind ?? null, ignored: result.ignored }) : null;

  return (
    <Sheet open={open} onClose={onClose} full title="Bank SMS">
      <div className="space-y-6 pb-4">
        <p className="text-[14px] text-ink-300">
          Forward your bank's SMS alerts to OmniFree: card payments become expenses, transfers you receive become income, ATM withdrawals move to your Cash
          account and the balance in the SMS updates the bank account.
        </p>

        {setup.error && <ErrorNote message={setup.error} onRetry={setup.reload} />}
        {s && !s.configured && (
          <div className="rounded-row bg-amber-400/10 px-4 py-3 text-[14px] text-amber-400">
            The webhook token is not set yet: add the vault key <code className="font-mono">{s.token_key}</code> to the hub, then come back here.
          </div>
        )}

        <ol className="space-y-5">
          <Step n={1} title="Install an SMS forwarder on your Android phone">
            <p className="text-[13px] text-ink-400">
              <span className="text-ink-200">SMS Forwarder</span> (free, open source: <span className="break-all">bogkonstantin/android_income_sms_gateway_webhook</span> on GitHub or F-Droid) or{' '}
              <span className="text-ink-200">SMS to URL Forwarder</span>. Allow it to read SMS and to run in the background (no battery optimisation).
            </p>
          </Step>

          <Step n={2} title="Add a webhook with this address (method POST)">
            <Segmented
              value={mode}
              onChange={setMode}
              options={[
                { value: 'query', label: 'Token in the URL' },
                { value: 'header', label: 'Token in a header' },
              ]}
            />
            {s ? (
              <>
                <CopyRow value={urlReal} shown={urlShown} label="Webhook URL" />
                {mode === 'header' && token && (
                  <div className="space-y-1">
                    <p className="text-[13px] text-ink-400">Add this header:</p>
                    <CopyRow value={`${s.header}: ${token}`} shown={`${s.header}: ${reveal ? token : maskSecret(token)}`} label="Header" />
                  </div>
                )}
                {token && (
                  <button type="button" onClick={() => setReveal((v) => !v)} className="press flex min-h-[40px] items-center gap-1.5 text-[13px] text-ink-400">
                    <Icon name={reveal ? 'lock' : 'eye'} size={15} />
                    {reveal ? 'Hide the token' : 'Show the token'}
                  </button>
                )}
                <p className="text-[13px] text-ink-400">JSON body (SMS Forwarder fills in the placeholders):</p>
                <CopyRow value={SMS_FORWARDER_TEMPLATE} shown={SMS_FORWARDER_TEMPLATE} label="Body template" />
              </>
            ) : (
              setup.loading && <div className="skeleton h-12 rounded-row" />
            )}
          </Step>

          <Step n={3} title="Only forward your bank's messages">
            <p className="text-[13px] text-ink-400">
              In the app's filter, set the sender to your bank's name as it appears in Messages, for example:
            </p>
            <div className="flex flex-wrap gap-1.5">
              {BANK_SENDERS.map((b) => (
                <Badge key={b}>{b}</Badge>
              ))}
            </div>
            <p className="text-[12px] text-ink-500">Codes (OTP), ads and other messages are ignored by the hub anyway, and code texts are never stored.</p>
          </Step>
        </ol>

        <section className="space-y-3 rounded-card bg-ink-900 p-4 ring-1 ring-inset ring-ink-800">
          <h3 className="font-display text-[17px] font-semibold text-ink-100">Test an SMS</h3>
          <p className="text-[13px] text-ink-400">Paste a real alert: you see how it is read, nothing is saved.</p>
          <Field label="Sender (optional)">
            <input value={sender} onChange={(e) => setSender(e.target.value)} placeholder="CIH BANK" className={inputClass} />
          </Field>
          <textarea
            value={sample}
            onChange={(e) => setSample(e.target.value)}
            rows={4}
            placeholder="Paiement par carte ***1234 de 245,50 MAD chez MARJANE le 20/10/2026. Solde: 3 456,78 MAD"
            aria-label="SMS text"
            className={cx(inputClass, 'min-h-[110px] py-3 text-[15px] leading-snug')}
          />
          <Button variant="primary" className="w-full" onClick={() => void test()} disabled={testing || !sample.trim()}>
            {testing ? 'Reading…' : 'Test'}
          </Button>
          {testError && <ErrorNote message={testError} />}
          {result && outcome && (
            <div className="screen-in-up space-y-2 rounded-row bg-ink-850 p-3 ring-1 ring-inset ring-ink-700">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={BADGE_TONE[outcome.tone]}>{outcome.label}</Badge>
                {p?.bank && <Badge>{p.bank}</Badge>}
                {p?.parser === 'llm' && <Badge tone="signal">read by AI</Badge>}
              </div>
              {p && p.amount !== null && (
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[14px]">
                  <dt className="text-ink-500">Amount</dt>
                  <dd className="font-display font-semibold text-ink-100">{formatMoney(p.amount, p.currency)}</dd>
                  {p.merchant && (
                    <>
                      <dt className="text-ink-500">{p.kind === 'income' ? 'From' : 'Where'}</dt>
                      <dd className="truncate text-ink-200">{p.merchant}</dd>
                    </>
                  )}
                  {p.date && (
                    <>
                      <dt className="text-ink-500">Date</dt>
                      <dd className="text-ink-200">{p.date}</dd>
                    </>
                  )}
                  {p.kind === 'expense' && (
                    <>
                      <dt className="text-ink-500">Category</dt>
                      <dd className="capitalize text-ink-200">{p.category}</dd>
                    </>
                  )}
                  {p.card && (
                    <>
                      <dt className="text-ink-500">Card</dt>
                      <dd className="text-ink-200">****{p.card}</dd>
                    </>
                  )}
                  {p.balance !== null && (
                    <>
                      <dt className="text-ink-500">Balance</dt>
                      <dd className="text-ink-200">{formatMoney(p.balance, p.balance_currency || 'MAD')}</dd>
                    </>
                  )}
                </dl>
              )}
            </div>
          )}
        </section>

        <section className="space-y-2">
          <div className="flex min-h-[44px] items-center justify-between px-1">
            <h3 className="font-display text-[17px] font-semibold text-ink-100">Last SMS received</h3>
            <IconButton
              icon="refresh"
              label="Refresh"
              onClick={() => {
                log.reload();
                announce('money');
              }}
            />
          </div>
          {log.error && <ErrorNote message={log.error} onRetry={log.reload} />}
          {log.loading && !log.data && <SkeletonList rows={3} />}
          {log.data && !log.data.length && <p className="px-1 text-[14px] text-ink-400">Nothing yet. Once the app is set up, your next bank SMS shows here.</p>}
          {log.data && log.data.length > 0 && (
            <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
              {log.data.map((m, i) => {
                const o = smsOutcome(m);
                return (
                  <div key={m.id} className="rise space-y-1 px-4 py-3" style={riseStyle(i)}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-[14px] text-ink-100">{m.bank || m.sender || 'Unknown sender'}</span>
                      <span className="shrink-0 text-[12px] text-ink-500">{when(m.received_at || m.created_at)}</span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={BADGE_TONE[o.tone]}>{o.label}</Badge>
                      {m.amount !== null && m.currency && <span className="font-display text-[14px] font-semibold text-ink-200">{formatMoney(m.amount, m.currency)}</span>}
                    </div>
                    {m.text && <p className="line-clamp-2 text-[12px] text-ink-500">{m.text}</p>}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </Sheet>
  );
}
