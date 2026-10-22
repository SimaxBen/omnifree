import { useEffect, useRef, useState, type ReactNode, type TouchEvent } from 'react';
import { registerSW } from 'virtual:pwa-register';
import AppSheets from './AppSheets';
import ErrorBoundary from './components/ErrorBoundary';
import { OfflineBanner } from './components/OfflineBanner';
import { Icon, type IconName } from './components/icons';
import { ConfirmProvider } from './components/Sheet';
import { StatusDot, cx, haptic } from './components/ui';
import { greeting, isoDay } from './lib/apps';
import { useHub } from './lib/hub';
import { NavProvider, hasRefreshers, runRefreshers, useNav, type Tab } from './lib/nav';
import { syncPush } from './lib/pushClient';
import { ScreenActionSlot } from './lib/screenAction';
import { UploadsProvider, useUploads } from './lib/uploads';
import AiView from './views/AiView';
import DriveScreen from './views/drive/DriveScreen';
import HomeView from './views/HomeView';
import MoreView, { MORE_PAGES } from './views/MoreView';
import NotesView from './views/notes/NotesView';

const NAV: { tab: Tab; label: string; icon: IconName }[] = [
  { tab: 'home', label: 'Home', icon: 'home' },
  { tab: 'notes', label: 'Notes', icon: 'note' },
  { tab: 'drive', label: 'Drive', icon: 'drive' },
  { tab: 'ai', label: 'AI', icon: 'spark' },
];

const TITLES: Record<Tab, string> = { home: '', notes: 'Notes', drive: 'Drive', ai: 'AI', more: 'More' };
const PULL_TRIGGER = 72;

function ProgressRing({ value }: { value: number }) {
  const c = 2 * Math.PI * 15;
  return (
    <svg width="36" height="36" viewBox="0 0 36 36" className="absolute -rotate-90" aria-hidden="true">
      <circle cx="18" cy="18" r="15" fill="none" stroke="#282E5C" strokeWidth="2.5" />
      <circle cx="18" cy="18" r="15" fill="none" stroke="#FFC24B" strokeWidth="2.5" strokeLinecap="round" strokeDasharray={`${c * value} ${c}`} className="transition-[stroke-dasharray] duration-base" />
    </svg>
  );
}

function BottomNav({ captureOpen, onCapture }: { captureOpen: boolean; onCapture: () => void }) {
  const { route, go } = useNav();
  const { overall } = useUploads();
  // Five columns: two tabs, the capture button, two tabs.
  const slots = [NAV[0], NAV[1], null, NAV[2], NAV[3]];
  const activeSlot = slots.findIndex((s) => s?.tab === route.tab);
  return (
    <nav className="pb-safe fixed inset-x-0 bottom-0 z-30 mx-auto max-w-xl" aria-label="Sections">
      <div className="relative mx-3 mb-2 rounded-[26px] bg-ink-900/95 shadow-[0_16px_40px_-12px_rgba(0,0,0,0.7)] ring-1 ring-inset ring-ink-800 backdrop-blur-xl">
        {activeSlot >= 0 && (
          <span
            aria-hidden="true"
            className="nav-blob absolute top-2 h-[52px] rounded-[18px] bg-signal/15"
            style={{ width: 'calc(20% - 12px)', left: `calc(${activeSlot * 20}% + 6px)` }}
          />
        )}
        <ul className="relative grid h-[68px] grid-cols-5 items-center">
          {slots.map((s, i) =>
            s ? (
              <li key={s.tab} className="flex justify-center">
                <button
                  type="button"
                  aria-current={route.tab === s.tab ? 'page' : undefined}
                  onClick={() => {
                    if (route.tab !== s.tab) haptic(6);
                    go(s.tab);
                  }}
                  className={cx(
                    'press relative flex h-[52px] w-full flex-col items-center justify-center gap-0.5 text-[11px] transition-colors duration-base',
                    route.tab === s.tab ? 'font-semibold text-signal' : 'text-ink-400',
                  )}
                >
                  <span className="relative grid h-9 w-9 place-items-center">
                    {s.tab === 'drive' && overall !== null && <ProgressRing value={overall} />}
                    <Icon name={s.icon} size={22} className={cx('transition-transform duration-slow ease-spring', route.tab === s.tab && '-translate-y-px scale-110')} />
                  </span>
                  {s.label}
                </button>
              </li>
            ) : (
              <li key={`capture-${i}`} className="flex justify-center">
                <button
                  type="button"
                  aria-label="Capture: voice, note, receipt or upload"
                  aria-expanded={captureOpen}
                  onClick={() => {
                    haptic(12);
                    onCapture();
                  }}
                  className="capture-btn -mt-7 grid h-[62px] w-[62px] place-items-center rounded-full bg-signal text-ink-950"
                >
                  <Icon name="plus" size={30} strokeWidth={2.4} />
                </button>
              </li>
            ),
          )}
        </ul>
      </div>
    </nav>
  );
}

function Toasts() {
  const { toasts, dismiss } = useHub();
  return (
    <div className="pointer-events-none fixed inset-x-0 z-[70] flex flex-col items-center gap-2 px-4" style={{ bottom: 'calc(var(--nav-h) + var(--safe-bottom) + 20px)' }} aria-live="polite">
      {toasts.map((t) => (
        <button
          type="button"
          key={t.id}
          onClick={() => dismiss(t.id)}
          className={cx(
            'pop pointer-events-auto flex min-h-[44px] max-w-md items-center gap-2 rounded-full px-4 py-2 text-left text-[14px] shadow-[0_12px_32px_-8px_rgba(0,0,0,0.7)] ring-1 ring-inset backdrop-blur-xl',
            t.tone === 'error' && 'bg-ink-850/95 text-danger-400 ring-danger-400/30',
            t.tone === 'ok' && 'bg-ink-850/95 text-ink-100 ring-mint-400/30',
            t.tone === 'info' && 'bg-ink-850/95 text-ink-100 ring-ink-700',
          )}
        >
          {t.tone === 'ok' && <Icon name="check" size={16} className="shrink-0 text-mint-400" />}
          {t.text}
        </button>
      ))}
    </div>
  );
}

function Screen({ children }: { children: ReactNode }) {
  const { direction } = useNav();
  return <div className={direction > 0 ? 'screen-in-right' : direction < 0 ? 'screen-in-left' : 'screen-in-up'}>{children}</div>;
}

function Shell() {
  const { connection, settings, client } = useHub();
  const { route, go, back } = useNav();
  const [captureOpen, setCaptureOpen] = useState(false);
  const [updateReady, setUpdateReady] = useState<null | (() => void)>(null);
  const [scrolled, setScrolled] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const arcRef = useRef<HTMLDivElement>(null);
  const pull = useRef<{ y: number; d: number } | null>(null);

  useEffect(() => {
    const update = registerSW({
      onNeedRefresh: () => setUpdateReady(() => () => void update(true)),
    });
  }, []);

  useEffect(() => {
    // Native web push: keep this device's subscription registered on the hub (no-op unless permission is granted).
    void syncPush(client, settings);
  }, [client, settings]);

  useEffect(() => {
    if (!settings.token) go('more', 'settings');
    // Only on first load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    mainRef.current?.scrollTo({ top: 0 });
    setScrolled(false);
  }, [route.tab, route.tab === 'more' ? route.sub : '']);

  const setArc = (d: number, animate: boolean) => {
    const el = arcRef.current;
    if (!el) return;
    el.style.transition = animate ? '' : 'none';
    el.style.transform = `translate3d(-50%, ${Math.min(d, 110) - 48}px, 0) rotate(${d * 3}deg)`;
    el.style.opacity = d > 4 ? String(Math.min(d / PULL_TRIGGER, 1)) : '0';
  };

  const onTouchStart = (e: TouchEvent) => {
    if (refreshing || !hasRefreshers() || (mainRef.current?.scrollTop ?? 0) > 0) return;
    pull.current = { y: e.touches[0].clientY, d: 0 };
  };
  const onTouchMove = (e: TouchEvent) => {
    const p = pull.current;
    if (!p) return;
    const dy = e.touches[0].clientY - p.y;
    if (dy <= 0 || (mainRef.current?.scrollTop ?? 0) > 0) {
      setArc(0, false);
      p.d = 0;
      return;
    }
    const d = dy * 0.5;
    if (d >= PULL_TRIGGER && p.d < PULL_TRIGGER) haptic(10);
    p.d = d;
    setArc(d, false);
  };
  const onTouchEnd = async () => {
    const p = pull.current;
    pull.current = null;
    if (!p) return;
    if (p.d < PULL_TRIGGER) {
      setArc(0, true);
      return;
    }
    setRefreshing(true);
    setArc(PULL_TRIGGER, true);
    await runRefreshers();
    setRefreshing(false);
    setArc(0, true);
  };

  const connTone = connection === 'open' || connection === 'polling' ? 'ok' : connection === 'connecting' ? 'warn' : 'bad';
  const morePage = route.tab === 'more' ? MORE_PAGES.find((p) => p.id === route.sub) : undefined;
  const title = route.tab === 'home' ? greeting() : morePage?.title ?? TITLES[route.tab];
  const subtitle =
    route.tab === 'home'
      ? new Date(`${isoDay()}T12:00:00Z`).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })
      : morePage?.subtitle;
  const showBack = route.tab === 'more' && !!route.sub;

  return (
    <AppSheets captureOpen={captureOpen} setCaptureOpen={setCaptureOpen}>
      <div className="relative mx-auto flex h-full max-w-xl flex-col">
        <main
          ref={mainRef}
          className="scroll-thin relative min-h-0 flex-1 overflow-y-auto overscroll-y-contain"
          onScroll={(e) => setScrolled((e.currentTarget.scrollTop ?? 0) > 44)}
          onTouchStart={onTouchStart}
          onTouchMove={onTouchMove}
          onTouchEnd={() => void onTouchEnd()}
        >
          <header className={cx('pt-safe sticky top-0 z-20 transition-colors duration-base', scrolled ? 'bg-ink-950/90 backdrop-blur-xl' : 'bg-transparent')}>
            <div className={cx('flex h-14 items-center gap-1 px-2 transition-[border-color] duration-base', scrolled ? 'border-b border-ink-800' : 'border-b border-transparent')}>
              {showBack ? (
                <button type="button" onClick={back} aria-label="Back to More" className="press grid h-11 w-11 place-items-center rounded-full text-ink-200 hover:bg-ink-800">
                  <Icon name="left" size={22} />
                </button>
              ) : (
                <img src="/icons/icon.svg" alt="" className="mx-2.5 h-7 w-7" />
              )}
              <p className={cx('min-w-0 flex-1 truncate font-display text-[17px] font-semibold text-ink-100 transition-all duration-base ease-out', scrolled || showBack ? 'translate-y-0 opacity-100' : 'translate-y-1 opacity-0')}>
                {title}
              </p>
              <button
                type="button"
                onClick={() => go('more', 'settings')}
                aria-label={`Hub ${connection === 'open' ? 'live' : connection}. Open settings`}
                className="press grid h-11 w-11 place-items-center rounded-full hover:bg-ink-800"
              >
                <StatusDot tone={connTone} pulse={connection === 'open'} />
              </button>
              <button
                type="button"
                onClick={() => go('more')}
                aria-label="More: expenses, links, budget, plugins, settings"
                aria-current={route.tab === 'more' ? 'page' : undefined}
                className={cx('press grid h-11 w-11 place-items-center rounded-full hover:bg-ink-800', route.tab === 'more' ? 'text-signal' : 'text-ink-300')}
              >
                <Icon name="grid" size={21} />
              </button>
            </div>
          </header>

          <div ref={arcRef} className="ptr-arc pointer-events-none absolute left-1/2 top-[calc(var(--safe-top)+56px)] z-10 grid h-10 w-10 place-items-center rounded-full bg-ink-850 opacity-0 shadow-lg ring-1 ring-ink-700" aria-hidden="true">
            <span className={cx('h-5 w-5 rounded-full border-2 border-ink-600 border-t-signal', refreshing && 'spin')} />
          </div>

          {updateReady && (
            <div className="screen-in-up mx-4 mb-2 flex items-center justify-between gap-3 rounded-row bg-signal/15 px-4 py-2 text-[14px] text-signal">
              <span>A new version is ready.</span>
              <button type="button" className="press min-h-[40px] font-semibold underline underline-offset-4" onClick={updateReady}>
                Reload
              </button>
            </div>
          )}

          <OfflineBanner />

          <div className="pb-nav px-4">
            {!showBack && (
              <div className="px-1 pb-4 pt-1">
                <div className="flex items-center gap-2">
                  <h1 className="min-w-0 flex-1 font-display text-[34px] font-bold leading-[1.05] text-ink-100">{title}</h1>
                  <ScreenActionSlot />
                </div>
                {subtitle && <p className="pt-1 text-[15px] text-ink-400">{subtitle}</p>}
              </div>
            )}
            {showBack && morePage?.subtitle && <p className="px-1 pb-3 text-[14px] text-ink-400">{morePage.subtitle}</p>}
            <ErrorBoundary key={`${route.tab}/${route.tab === 'more' ? route.sub : ''}`}>
              <Screen key={`${route.tab}/${route.tab === 'more' ? route.sub : ''}`}>
                {route.tab === 'home' && <HomeView />}
                {route.tab === 'notes' && <NotesView />}
                {route.tab === 'drive' && <DriveScreen />}
                {route.tab === 'ai' && <AiView />}
                {route.tab === 'more' && <MoreView />}
              </Screen>
            </ErrorBoundary>
          </div>
        </main>

        <BottomNav captureOpen={captureOpen} onCapture={() => setCaptureOpen(true)} />
        <Toasts />
      </div>
    </AppSheets>
  );
}

export default function App() {
  return (
    <NavProvider>
      <ConfirmProvider>
        <UploadsProvider>
          <Shell />
        </UploadsProvider>
      </ConfirmProvider>
    </NavProvider>
  );
}
