// Pure helpers for the Notifications page and the Schedules templates. No React, no I/O.

import type { NotifyKind } from './types';

/** What each push kind does, in the order the Notifications page lists them. */
export const NOTIFY_KINDS: { kind: NotifyKind; label: string; detail: string }[] = [
  { kind: 'morning', label: 'Morning', detail: 'Tasks due today, overdue count, yesterday’s spend' },
  { kind: 'evening', label: 'Evening', detail: 'Tasks still open today, a nudge if no expense was logged' },
  { kind: 'weekly', label: 'Weekly summary', detail: 'Last week’s spending, tasks and notes (Mondays)' },
  { kind: 'budget', label: 'Free-tier budget', detail: 'Plugins that newly reach 80 % or 100 % of a limit' },
  { kind: 'month', label: 'Month-end recap', detail: 'Spending this month, recurring money when set up' },
  { kind: 'inbox', label: 'Email receipts', detail: 'Right after an import creates new expenses' },
];

export interface ScheduleTemplate {
  id: string;
  /** Schedule name created on the cron provider. */
  name: string;
  label: string;
  description: string;
  /** 5-field cron expression, UTC. */
  schedule: string;
  /** Path on the hub, appended to the hub URL. */
  path: string;
}

/** Ready-made schedules (UTC; Morocco is UTC+1, so 07:00 UTC is 08:00 there). All call this hub with the token. */
export const SCHEDULE_TEMPLATES: ScheduleTemplate[] = [
  {
    id: 'morning', name: 'push-morning', label: 'Morning push', schedule: '0 7 * * *', path: '/notify/run?kind=morning',
    description: 'Daily 08:00 Morocco: tasks due today and yesterday’s spend',
  },
  {
    id: 'evening', name: 'push-evening', label: 'Evening push', schedule: '0 19 * * *', path: '/notify/run?kind=evening',
    description: 'Daily 20:00 Morocco: open tasks, log-your-expenses nudge',
  },
  {
    id: 'weekly', name: 'push-weekly', label: 'Weekly summary push', schedule: '30 7 * * 1', path: '/notify/run?kind=weekly',
    description: 'Mondays 08:30 Morocco: last week in one notification',
  },
  {
    id: 'budget', name: 'push-budget', label: 'Free-tier budget check', schedule: '0 6,18 * * *', path: '/notify/run?kind=budget',
    description: 'Twice a day: pushes quotas that newly hit warn or over',
  },
  {
    id: 'month', name: 'push-month', label: 'Month-end money recap', schedule: '0 8 28 * *', path: '/notify/run?kind=month',
    description: 'Day 28, 09:00 Morocco: the month’s spending so far',
  },
];

/** Form values for a template: POST to this hub, no body, "Send my hub token" on. */
export function templateJob(t: ScheduleTemplate, hubUrl: string): { name: string; schedule: string; target: string; method: string; body: string; withToken: boolean } {
  return { name: t.name, schedule: t.schedule, target: `${hubUrl.replace(/\/+$/, '')}${t.path}`, method: 'POST', body: '', withToken: true };
}

/** Hides a secret except its first and last characters ("omni••••••x9k2"); short values are fully hidden. */
export function maskSecret(value: string | null | undefined, keep = 4): string {
  if (!value) return '';
  if (value.length <= keep * 2 + 2) return '•'.repeat(Math.max(value.length, 6));
  return `${value.slice(0, keep)}${'•'.repeat(8)}${value.slice(-keep)}`;
}

/** Hides the token segment of a calendar feed URL (`.../calendar/<token>.ics`). */
export function maskFeedUrl(url: string | null | undefined): string {
  if (!url) return '';
  return url.replace(/\/calendar\/([^/]+)\.ics$/, (_m, token: string) => `/calendar/${maskSecret(token)}.ics`);
}

/** `ntfy://` deep link that opens the ntfy app subscribed to the topic (Android); null without a topic. */
export function ntfySubscribeLink(server: string | null | undefined, topic: string | null | undefined): string | null {
  if (!topic) return null;
  const host = (server || 'https://ntfy.sh').replace(/^https?:\/\//, '').replace(/\/+$/, '');
  return `ntfy://${host}/${encodeURIComponent(topic)}`;
}
