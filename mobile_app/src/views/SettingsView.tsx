import { useState, type FormEvent } from 'react';
import { Button, Card, SectionTitle, StatusDot, cx, inputClass } from '../components/ui';
import { HubClient } from '../lib/api';
import { errorText, useHub } from '../lib/hub';
import { normalizeHubUrl } from '../lib/settings';

type TestState = { state: 'idle' } | { state: 'testing' } | { state: 'ok'; detail: string } | { state: 'fail'; detail: string };

export default function SettingsView({ onDone }: { onDone: () => void }) {
  const { settings, updateSettings, notify, connection, events } = useHub();
  const [hubUrl, setHubUrl] = useState(settings.hubUrl);
  const [token, setToken] = useState(settings.token);
  const [reveal, setReveal] = useState(false);
  const [test, setTest] = useState<TestState>({ state: 'idle' });

  const runTest = async () => {
    setTest({ state: 'testing' });
    const probe = new HubClient({ hubUrl: normalizeHubUrl(hubUrl), token: token.trim() });
    try {
      // /health proves reachability; /plugins proves the token is accepted.
      await probe.health();
      const plugins = await probe.plugins();
      setTest({ state: 'ok', detail: `Connected. ${plugins.length} plugins registered.` });
    } catch (err) {
      setTest({ state: 'fail', detail: errorText(err) });
    }
  };

  const save = (e: FormEvent) => {
    e.preventDefault();
    const persisted = updateSettings({ hubUrl: normalizeHubUrl(hubUrl), token: token.trim() });
    notify(persisted ? 'Settings saved' : 'Saved for this session only: this browser blocks local storage', persisted ? 'ok' : 'error');
    onDone();
  };

  return (
    <div className="space-y-5">
      <form onSubmit={save} className="space-y-4">
        <div>
          <SectionTitle>Hub connection</SectionTitle>
          <Card className="space-y-3 p-3">
            <label className="block space-y-1 text-sm text-ink-200">
              Hub URL
              <input
                className={inputClass}
                type="url"
                inputMode="url"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                placeholder="https://hub-xxxx.run.app"
                value={hubUrl}
                onChange={(e) => setHubUrl(e.target.value)}
              />
            </label>
            <label className="block space-y-1 text-sm text-ink-200">
              API token
              <div className="flex gap-2">
                <input
                  className={cx(inputClass, 'font-mono')}
                  type={reveal ? 'text' : 'password'}
                  autoComplete="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  placeholder="HUB_API_TOKEN"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
                <Button variant="ghost" onClick={() => setReveal((v) => !v)}>
                  {reveal ? 'Hide' : 'Show'}
                </Button>
              </div>
            </label>
            <p className="text-xs text-ink-500">
              Stored only in this browser&apos;s local storage. The token is the hub&apos;s <span className="font-mono">HUB_API_TOKEN</span> from the vault.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button icon="pulse" onClick={() => void runTest()} disabled={test.state === 'testing'}>
                {test.state === 'testing' ? 'Testing...' : 'Test connection'}
              </Button>
              {test.state === 'ok' && <span className="text-xs text-signal">{test.detail}</span>}
              {test.state === 'fail' && <span className="break-words text-xs text-danger-400">{test.detail}</span>}
            </div>
          </Card>
        </div>
        <Button type="submit" variant="primary" className="w-full">
          Save
        </Button>
      </form>

      <div>
        <SectionTitle>Live events</SectionTitle>
        <Card className="p-3">
          <p className="mb-2 flex items-center gap-2 text-sm text-ink-200">
            <StatusDot tone={connection === 'open' ? 'ok' : connection === 'connecting' ? 'warn' : 'bad'} />
            WebSocket {connection}
          </p>
          <ul className="scroll-thin max-h-48 space-y-0.5 overflow-y-auto font-mono text-[11px] text-ink-400">
            {events.recent.length === 0 && <li>No events received yet.</li>}
            {events.recent.slice(0, 40).map((e) => (
              <li key={e.id} className="truncate">
                <span className="text-ink-500">{new Date(e.ts).toLocaleTimeString()}</span> <span className="text-ink-200">{e.type}</span>
                {e.provider && <span> {e.provider}</span>}
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <p className="text-center font-mono text-[11px] text-ink-600">OmniFree Control v0.1.0</p>
    </div>
  );
}
