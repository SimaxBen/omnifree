// Cron expressions for the Schedules screen: presets and a plain-English reading of common patterns.
// Hub schedules are 5-field expressions evaluated in UTC.

export interface SchedulePreset {
  id: string;
  label: string;
  schedule: string;
}

export const SCHEDULE_PRESETS: SchedulePreset[] = [
  { id: '15min', label: 'Every 15 min', schedule: '*/15 * * * *' },
  { id: 'hourly', label: 'Hourly', schedule: '0 * * * *' },
  { id: 'daily', label: 'Daily 08:00 UTC', schedule: '0 8 * * *' },
  { id: 'weekly', label: 'Weekly Mon 08:00 UTC', schedule: '0 8 * * 1' },
];

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

const FIELD = /^(\*|\d+(-\d+)?)(\/\d+)?(,(\*|\d+(-\d+)?)(\/\d+)?)*$/;
const RANGES: [number, number][] = [
  [0, 59],
  [0, 23],
  [1, 31],
  [1, 12],
  [0, 7],
];

/** Shallow syntax check: 5 fields of numbers, ranges, lists, steps and `*`, with values in range. */
export function isValidCron(expr: string): boolean {
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return false;
  return fields.every((f, i) => {
    if (!FIELD.test(f)) return false;
    const [min, max] = RANGES[i];
    return (f.match(/\d+/g) ?? []).every((n, j, all) => {
      // Step values (`/n`) only need to be positive.
      const isStep = f.includes('/') && j === all.length - 1 && f.endsWith(`/${n}`);
      const v = Number(n);
      return isStep ? v > 0 : v >= min && v <= max;
    });
  });
}

const pad = (n: number) => String(n).padStart(2, '0');
const isNum = (f: string) => /^\d+$/.test(f);

/**
 * Reads common shapes ("every 15 minutes", "daily at 08:00 UTC", "Mondays at 08:00 UTC", ...).
 * Returns null for anything else, so the UI shows only the raw expression.
 */
export function describeCron(expr: string): string | null {
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5 || !isValidCron(expr)) return null;
  const [min, hour, dom, mon, dow] = fields;
  const anyDate = dom === '*' && mon === '*';

  if (anyDate && dow === '*') {
    if (min === '*' && hour === '*') return 'Every minute';
    const everyMin = /^\*\/(\d+)$/.exec(min);
    if (everyMin && hour === '*') return Number(everyMin[1]) === 1 ? 'Every minute' : `Every ${everyMin[1]} minutes`;
    if (isNum(min) && hour === '*') return Number(min) === 0 ? 'Hourly' : `Hourly at :${pad(Number(min))}`;
    const everyHour = /^\*\/(\d+)$/.exec(hour);
    if (isNum(min) && everyHour) {
      const n = Number(everyHour[1]);
      return `${n === 1 ? 'Every hour' : `Every ${n} hours`}${Number(min) ? ` at :${pad(Number(min))}` : ''}`;
    }
  }
  if (anyDate && dow === '*' && isNum(min) && /^\d+(,\d+)+$/.test(hour)) {
    const times = hour.split(',').map((h) => `${pad(Number(h))}:${pad(Number(min))}`);
    return `Daily at ${times.slice(0, -1).join(', ')} and ${times[times.length - 1]} UTC`;
  }
  if (!isNum(min) || !isNum(hour)) return null;
  const at = `${pad(Number(hour))}:${pad(Number(min))} UTC`;
  if (anyDate && dow === '*') return `Daily at ${at}`;
  if (anyDate && isNum(dow)) return `${DAYS[Number(dow) % 7]}s at ${at}`;
  if (anyDate && dow === '1-5') return `Weekdays at ${at}`;
  if (isNum(dom) && mon === '*' && dow === '*') return `Monthly on day ${Number(dom)} at ${at}`;
  return null;
}

/** Preset id matching `schedule`, or 'custom'. */
export function presetFor(schedule: string): string {
  const norm = schedule.trim().split(/\s+/).join(' ');
  return SCHEDULE_PRESETS.find((p) => p.schedule === norm)?.id ?? 'custom';
}

/**
 * Validates an optional JSON request body typed by the user. Returns the text to send (the
 * hub stores the body as a string) or an error message.
 */
export function checkJsonBody(text: string): { body?: string; error?: string } {
  const trimmed = text.trim();
  if (!trimmed) return {};
  try {
    JSON.parse(trimmed);
    return { body: trimmed };
  } catch (err) {
    return { error: `Body is not valid JSON (${err instanceof Error ? err.message : String(err)})` };
  }
}
