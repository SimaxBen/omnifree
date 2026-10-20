import { useEffect, useState } from 'react';
import { registerSW } from 'virtual:pwa-register';
import { Icon, type IconName } from './components/icons';
import ErrorBoundary from './components/ErrorBoundary';
import { cx, IconButton, StatusDot } from './components/ui';
import { useHub } from './lib/hub';
import { readPref, writePref } from './lib/settings';
import DatabaseView from './views/DatabaseView';
import DriveView from './views/DriveView';
import LlmView from './views/llm/LlmView';
import NetworkView from './views/NetworkView';
import SettingsView from './views/SettingsView';
import SwitchboardView from './views/SwitchboardView';

type Tab = 'llm' | 'drive' | 'network' | 'db' | 'plugins';

const TABS: { id: Tab; label: string; title: string; icon: IconName }[] = [
  { id: 'llm', label: 'Pool', title: 'LLM Pool & Agent', icon: 'chat' },
  { id: 'drive', label: 'Drive', title: 'Unified Drive', icon: 'drive' },
  { id: 'network', label: 'Network', title: 'DNS & Network', icon: 'network' },
  { id: 'db', label: 'Data', title: 'Database Explorer', icon: 'database' },
  { id: 'plugins', label: 'Plugins', title: 'Plugin Switchboard', icon: 'switchboard' },
];

export default function App() {
  const { connection, toasts, dismiss, settings } = useHub();
  const [tab, setTab] = useState<Tab>(() => {
    const saved = readPref<string>('tab', 'llm');
    return TABS.some((t) => t.id === saved) ? (saved as Tab) : 'llm';
  });
  const [showSettings, setShowSettings] = useState(() => !settings.token);
  const [updateReady, setUpdateReady] = useState<null | (() => void)>(null);

  useEffect(() => {
    writePref('tab', tab);
  }, [tab]);

  useEffect(() => {
    const update = registerSW({
      onNeedRefresh: () => setUpdateReady(() => () => void update(true)),
    });
  }, []);

  const current = TABS.find((t) => t.id === tab) ?? TABS[0];
  const connTone = connection === 'open' || connection === 'polling' ? 'ok' : connection === 'connecting' ? 'warn' : 'bad';

  return (
    <div className="backdrop-grid mx-auto flex h-full max-w-3xl flex-col">
      <header className="pt-safe sticky top-0 z-20 border-b border-ink-800 bg-ink-950/90 backdrop-blur">
        <div className="flex h-14 items-center gap-3 px-4">
          <img src="/icons/icon.svg" alt="" className="h-7 w-7" />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[15px] font-semibold leading-tight text-ink-100">{showSettings ? 'Settings' : current.title}</h1>
            <p className="flex items-center gap-1.5 font-mono text-[11px] text-ink-400">
              <StatusDot tone={connTone} pulse={connection === 'open'} />
              <span className="truncate">{connection === 'open' ? 'live' : connection} · {settings.hubUrl.replace(/^https?:\/\//, '')}</span>
            </p>
          </div>
          <IconButton
            icon={showSettings ? 'x' : 'settings'}
            label={showSettings ? 'Close settings' : 'Open settings'}
            onClick={() => setShowSettings((v) => !v)}
          />
        </div>
      </header>

      {updateReady && (
        <div className="mx-4 mt-3 flex items-center justify-between gap-3 rounded-lg border border-signal/40 bg-signal/10 px-3 py-2 text-sm text-signal">
          <span>A new version is ready.</span>
          <button type="button" className="font-medium underline underline-offset-2" onClick={updateReady}>
            Reload
          </button>
        </div>
      )}

      <main className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 pb-6 pt-4">
        <ErrorBoundary key={showSettings ? 'settings' : tab}>
          {showSettings ? (
            <SettingsView onDone={() => setShowSettings(false)} />
          ) : (
            <>
              {tab === 'llm' && <LlmView />}
              {tab === 'drive' && <DriveView />}
              {tab === 'network' && <NetworkView />}
              {tab === 'db' && <DatabaseView />}
              {tab === 'plugins' && <SwitchboardView />}
            </>
          )}
        </ErrorBoundary>
      </main>

      <nav className="pb-safe sticky bottom-0 z-20 border-t border-ink-800 bg-ink-950/95 backdrop-blur" aria-label="Sections">
        <ul className="grid grid-cols-5">
          {TABS.map((t) => {
            const active = !showSettings && t.id === tab;
            return (
              <li key={t.id}>
                <button
                  type="button"
                  aria-current={active ? 'page' : undefined}
                  onClick={() => {
                    setTab(t.id);
                    setShowSettings(false);
                  }}
                  className={cx(
                    'relative flex h-16 w-full flex-col items-center justify-center gap-1 text-[11px] transition-colors',
                    active ? 'text-signal' : 'text-ink-400 hover:text-ink-200',
                  )}
                >
                  {active && <span className="absolute top-0 h-0.5 w-8 rounded-full bg-signal" />}
                  <Icon name={t.icon} size={22} />
                  {t.label}
                </button>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="pointer-events-none fixed inset-x-0 bottom-20 z-30 flex flex-col items-center gap-2 px-4" aria-live="polite">
        {toasts.map((t) => (
          <button
            type="button"
            key={t.id}
            onClick={() => dismiss(t.id)}
            className={cx(
              'pointer-events-auto max-w-md rounded-lg border px-3 py-2 text-left text-sm shadow-lg backdrop-blur',
              t.tone === 'error' && 'border-danger-400/40 bg-ink-900/95 text-danger-400',
              t.tone === 'ok' && 'border-signal/40 bg-ink-900/95 text-signal',
              t.tone === 'info' && 'border-ink-600 bg-ink-900/95 text-ink-100',
            )}
          >
            {t.text}
          </button>
        ))}
      </div>
    </div>
  );
}
