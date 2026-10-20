import type { ReactNode } from 'react';
import type { IconName } from '../components/icons';
import { Badge, ListRow, riseStyle } from '../components/ui';
import { useHub, useHubData } from '../lib/hub';
import { useNav } from '../lib/nav';
import BudgetView from './BudgetView';
import DatabaseView from './DatabaseView';
import ExpensesView from './money/ExpensesView';
import LinksView from './LinksView';
import NetworkView from './NetworkView';
import SchedulesView from './SchedulesView';
import SettingsView from './SettingsView';
import SwitchboardView from './SwitchboardView';

export interface MorePage {
  id: string;
  title: string;
  short: string;
  subtitle: string;
  icon: IconName;
  group: 'life' | 'cloud' | 'app';
}

export const MORE_PAGES: MorePage[] = [
  { id: 'expenses', title: 'Expenses', short: 'Expenses', subtitle: 'Receipts and spending by month', icon: 'wallet', group: 'life' },
  { id: 'links', title: 'Links & drops', short: 'Links', subtitle: 'Short links, QR codes and file drops', icon: 'link', group: 'life' },
  { id: 'budget', title: 'Free-tier budget', short: 'Budget', subtitle: 'How close every plugin is to its limits', icon: 'gauge', group: 'cloud' },
  { id: 'schedules', title: 'Schedules', short: 'Schedules', subtitle: 'Cron jobs on QStash and GitHub Actions', icon: 'clock', group: 'cloud' },
  { id: 'plugins', title: 'Plugins', short: 'Plugins', subtitle: 'Turn providers on or off and set their order', icon: 'switchboard', group: 'cloud' },
  { id: 'network', title: 'Network', short: 'Network', subtitle: 'DNS blocking and Tailscale nodes', icon: 'network', group: 'cloud' },
  { id: 'data', title: 'Databases', short: 'Data', subtitle: 'Browse tables and run queries', icon: 'database', group: 'cloud' },
  { id: 'settings', title: 'Settings', short: 'Settings', subtitle: 'Hub address and access token', icon: 'settings', group: 'app' },
];

function Page({ id }: { id: string }): ReactNode {
  const { go } = useNav();
  switch (id) {
    case 'expenses':
      return <ExpensesView />;
    case 'links':
      return <LinksView />;
    case 'budget':
      return <BudgetView />;
    case 'schedules':
      return <SchedulesView />;
    case 'plugins':
      return <SwitchboardView />;
    case 'network':
      return <NetworkView />;
    case 'data':
      return <DatabaseView />;
    case 'settings':
      return <SettingsView onDone={() => go('home')} />;
    default:
      return null;
  }
}

const GROUPS: { id: MorePage['group']; title: string }[] = [
  { id: 'life', title: 'Personal' },
  { id: 'cloud', title: 'Your cloud' },
  { id: 'app', title: 'This app' },
];

export default function MoreView() {
  const { route, go } = useNav();
  const { connection, settings } = useHub();
  const budget = useHubData((c, s) => c.budget(s), []);
  if (route.sub && MORE_PAGES.some((p) => p.id === route.sub)) return <Page id={route.sub} />;

  const s = budget.data?.summary;
  const trailing = (id: string) => {
    if (id === 'budget' && s && (s.over || s.warn)) return <Badge tone={s.over ? 'bad' : 'warn'}>{s.over ? `${s.over} over` : `${s.warn} close`}</Badge>;
    if (id === 'settings') return <Badge tone={connection === 'open' || connection === 'polling' ? 'ok' : 'warn'}>{settings.token ? connection : 'no token'}</Badge>;
    return undefined;
  };

  let n = 0;
  return (
    <div className="space-y-6">
      {GROUPS.map((g) => (
        <section key={g.id}>
          <h2 className="px-1 pb-2 font-display text-[15px] font-semibold text-ink-400">{g.title}</h2>
          <div className="divide-y divide-ink-800 overflow-hidden rounded-card bg-ink-900 ring-1 ring-inset ring-ink-800">
            {MORE_PAGES.filter((p) => p.group === g.id).map((p) => {
              const t = trailing(p.id);
              return (
                <div key={p.id} className="rise" style={riseStyle(n++)}>
                  <ListRow
                    icon={p.icon}
                    title={p.title}
                    detail={p.subtitle}
                    onClick={() => go('more', p.id)}
                    trailing={t && <span className="flex items-center gap-2">{t}</span>}
                  />
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
