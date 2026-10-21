import { describe, expect, it } from 'vitest';
import { describeCron, isValidCron } from '../cron';
import { NOTIFY_KINDS, SCHEDULE_TEMPLATES, maskFeedUrl, maskSecret, ntfySubscribeLink, templateJob } from '../notify';

describe('notification schedule templates', () => {
  it('has the five templates with valid UTC schedules and readable descriptions', () => {
    expect(SCHEDULE_TEMPLATES.map((t) => [t.id, t.schedule])).toEqual([
      ['morning', '0 7 * * *'],
      ['evening', '0 19 * * *'],
      ['weekly', '30 7 * * 1'],
      ['budget', '0 6,18 * * *'],
      ['month', '0 8 28 * *'],
    ]);
    for (const t of SCHEDULE_TEMPLATES) {
      expect(isValidCron(t.schedule)).toBe(true);
      expect(describeCron(t.schedule)).not.toBeNull();
      expect(t.path).toBe(`/notify/run?kind=${t.id}`);
    }
    expect(describeCron('0 6,18 * * *')).toBe('Daily at 06:00 and 18:00 UTC');
    expect(describeCron('0 6,12,18 * * *')).toBe('Daily at 06:00, 12:00 and 18:00 UTC');
    expect(SCHEDULE_TEMPLATES.some((t) => t.path.includes('inbox'))).toBe(false); // the email import template is gone
  });

  it('fills a POST to this hub with the token on', () => {
    expect(templateJob(SCHEDULE_TEMPLATES[0], 'https://api.ilyasouhnine.tech/')).toEqual({
      name: 'push-morning',
      schedule: '0 7 * * *',
      target: 'https://api.ilyasouhnine.tech/notify/run?kind=morning',
      method: 'POST',
      body: '',
      withToken: true,
    });
  });

  it('lists every server-side kind once', () => {
    expect(NOTIFY_KINDS.map((k) => k.kind)).toEqual(['morning', 'evening', 'weekly', 'budget', 'month', 'inbox']);
  });
});

describe('secret masking', () => {
  it('masks topics and feed tokens', () => {
    expect(maskSecret('omnifree-topic-x9k2')).toBe('omni••••••••x9k2');
    expect(maskSecret('short')).toBe('••••••');
    expect(maskSecret(null)).toBe('');
    expect(maskFeedUrl('https://api.example/calendar/abcdefghijklmnop.ics')).toBe('https://api.example/calendar/abcd••••••••mnop.ics');
    expect(maskFeedUrl(undefined)).toBe('');
  });

  it('builds the ntfy app deep link', () => {
    expect(ntfySubscribeLink(null, 'my topic')).toBe('ntfy://ntfy.sh/my%20topic');
    expect(ntfySubscribeLink('https://push.example.org/', 't')).toBe('ntfy://push.example.org/t');
    expect(ntfySubscribeLink('https://ntfy.sh', null)).toBeNull();
  });
});
