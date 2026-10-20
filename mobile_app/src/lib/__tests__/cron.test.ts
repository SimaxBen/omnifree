import { describe, expect, it, vi } from 'vitest';
import { SCHEDULE_PRESETS, checkJsonBody, describeCron, isValidCron, presetFor } from '../cron';
import { copyText } from '../dom';

describe('cron helpers', () => {
  it('reads every preset in plain English', () => {
    expect(SCHEDULE_PRESETS.map((p) => describeCron(p.schedule))).toEqual([
      'Every 15 minutes',
      'Hourly',
      'Daily at 08:00 UTC',
      'Mondays at 08:00 UTC',
    ]);
  });

  it('reads other common shapes and gives up on the rest', () => {
    expect(describeCron('* * * * *')).toBe('Every minute');
    expect(describeCron('30 * * * *')).toBe('Hourly at :30');
    expect(describeCron('0 */6 * * *')).toBe('Every 6 hours');
    expect(describeCron('15 */2 * * *')).toBe('Every 2 hours at :15');
    expect(describeCron('5 7 * * 0')).toBe('Sundays at 07:05 UTC');
    expect(describeCron('0 9 * * 1-5')).toBe('Weekdays at 09:00 UTC');
    expect(describeCron('0 3 1 * *')).toBe('Monthly on day 1 at 03:00 UTC');
    expect(describeCron('0 3 1 1 *')).toBeNull();
    expect(describeCron('nonsense')).toBeNull();
  });

  it('validates 5-field expressions', () => {
    expect(isValidCron('*/15 * * * *')).toBe(true);
    expect(isValidCron('0 8 * * 1')).toBe(true);
    expect(isValidCron('0,30 8-18/2 * * 1-5')).toBe(true);
    expect(isValidCron('0 8 * *')).toBe(false);
    expect(isValidCron('60 * * * *')).toBe(false);
    expect(isValidCron('0 24 * * *')).toBe(false);
    expect(isValidCron('*/0 * * * *')).toBe(false);
    expect(isValidCron('@daily')).toBe(false);
  });

  it('maps schedules back to presets', () => {
    expect(presetFor('0  8 * * 1')).toBe('weekly');
    expect(presetFor('1 2 3 4 5')).toBe('custom');
  });

  it('checks the optional JSON body', () => {
    expect(checkJsonBody('  ')).toEqual({});
    expect(checkJsonBody(' {"a": 1} ')).toEqual({ body: '{"a": 1}' });
    expect(checkJsonBody('{a:1}').error).toMatch(/^Body is not valid JSON/);
  });
});

describe('copyText', () => {
  it('uses the Clipboard API, and reports failure when neither path is available', async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    expect(await copyText('hello')).toBe(true);
    expect(writeText).toHaveBeenCalledWith('hello');
    vi.stubGlobal('navigator', {});
    expect(await copyText('hello')).toBe(false);
    vi.unstubAllGlobals();
  });
});
