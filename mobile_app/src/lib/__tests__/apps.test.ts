import { describe, expect, it } from 'vitest';
import {
  addDays,
  budgetAlerts,
  categoryBreakdown,
  currenciesByWeight,
  dueInfo,
  formatMoney,
  groupBudget,
  isoDay,
  monthLabel,
  shiftMonth,
  sortTasks,
} from '../apps';
import { parseBlocks, parseInline, plainText, toggleCheckbox } from '../markdown';
import type { BudgetItem, Task } from '../types';

describe('dates', () => {
  it('formats the day in the home time zone', () => {
    // 23:30 UTC on Oct 20 is already Oct 21 in Casablanca (UTC+1).
    expect(isoDay(new Date('2026-10-20T23:30:00Z'))).toBe('2026-10-21');
    expect(isoDay(new Date('2026-10-20T23:30:00Z'), 'UTC')).toBe('2026-10-20');
  });

  it('labels due dates relative to today', () => {
    const today = '2026-10-20';
    expect(dueInfo(null, today)).toEqual({ label: 'No date', tone: 'none' });
    expect(dueInfo('2026-10-20', today).tone).toBe('today');
    expect(dueInfo('2026-10-21', today).label).toBe('Tomorrow');
    expect(dueInfo('2026-10-19', today)).toEqual({ label: 'Yesterday', tone: 'overdue' });
    expect(dueInfo('2026-10-15', today).label).toBe('5 days late');
    expect(dueInfo('2026-10-23', today)).toEqual({ label: 'Friday', tone: 'soon' });
    expect(dueInfo('2026-11-30', today)).toEqual({ label: '30 Nov', tone: 'later' });
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
  });

  it('shifts and names months', () => {
    expect(shiftMonth('2026-01', -1)).toBe('2025-12');
    expect(shiftMonth('2026-12', 1)).toBe('2027-01');
    expect(monthLabel('2026-10')).toBe('October 2026');
  });
});

describe('tasks', () => {
  const t = (id: string, over: Partial<Task>): Task => ({
    id,
    title: id,
    details: null,
    due: null,
    done: false,
    priority: 'normal',
    note_id: null,
    created_at: '2026-10-01',
    done_at: null,
    ...over,
  });
  it('sorts open first, by due date with undated last, then priority', () => {
    const sorted = sortTasks([
      t('done', { done: true, due: '2026-01-01' }),
      t('nodate', {}),
      t('late-low', { due: '2026-10-01', priority: 'low' }),
      t('late-high', { due: '2026-10-01', priority: 'high' }),
      t('soon', { due: '2026-10-25' }),
    ]);
    expect(sorted.map((x) => x.id)).toEqual(['late-high', 'late-low', 'soon', 'nodate', 'done']);
  });
});

describe('money', () => {
  it('formats with the currency code', () => {
    expect(formatMoney(245.5, 'MAD')).toBe('MAD 245.50');
    expect(formatMoney(3, 'XXX-bad')).toBe('3.00 XXX-bad');
  });

  it('breaks a currency total down by category', () => {
    const slices = categoryBreakdown({ food: { MAD: 30, EUR: 5 }, groceries: { MAD: 70 }, travel: { EUR: 100 } }, 'MAD');
    expect(slices).toEqual([
      { category: 'groceries', amount: 70, share: 0.7 },
      { category: 'food', amount: 30, share: 0.3 },
    ]);
    expect(currenciesByWeight({ EUR: 10, MAD: 300 })).toEqual(['MAD', 'EUR']);
  });
});

describe('budget', () => {
  const item = (provider: string, kind: string, status: BudgetItem['status'], fraction: number | null): BudgetItem => ({
    provider,
    kind,
    group: kind,
    metric: 'requests',
    window: 'day',
    used: null,
    limit: null,
    unit: 'requests',
    fraction,
    status,
    reset_at: null,
    source: 'router',
  });
  const items = [item('groq', 'llm', 'ok', 0.2), item('r2', 'storage', 'over', 1), item('gemini', 'llm', 'warn', 0.85), item('neon', 'db', 'unknown', null)];

  it('groups by kind, worst group first', () => {
    const groups = groupBudget(items);
    expect(groups.map((g) => g.kind)).toEqual(['storage', 'llm', 'db']);
    expect(groups[1].items.map((i) => i.provider)).toEqual(['gemini', 'groq']);
    expect(groups[1].worst).toBe('warn');
  });

  it('lists alerts, over before warn', () => {
    expect(budgetAlerts(items).map((i) => i.provider)).toEqual(['r2', 'gemini']);
  });
});

describe('markdown', () => {
  it('parses blocks', () => {
    const blocks = parseBlocks('# Title\n\nSome *text*\nmore\n\n- [ ] milk\n- [x] eggs\n\n1. one\n2. two\n\n> quoted\n\n```\ncode\n```\n---');
    expect(blocks.map((b) => b.t)).toEqual(['heading', 'para', 'list', 'list', 'quote', 'code', 'hr']);
    expect(blocks[2]).toEqual({
      t: 'list',
      ordered: false,
      start: 1,
      items: [
        { text: 'milk', checked: false, children: [] },
        { text: 'eggs', checked: true, children: [] },
      ],
    });
    expect(blocks[3]).toMatchObject({ ordered: true, items: [{ text: 'one', checked: null }, { text: 'two', checked: null }] });
    expect(blocks[1]).toEqual({ t: 'para', text: 'Some *text*\nmore' });
  });

  it('parses inline marks, safe links and citations', () => {
    expect(parseInline('a **b** *c* `d` [e](https://x.y) [1][2]')).toEqual([
      { t: 'text', v: 'a ' },
      { t: 'strong', c: [{ t: 'text', v: 'b' }] },
      { t: 'text', v: ' ' },
      { t: 'em', c: [{ t: 'text', v: 'c' }] },
      { t: 'text', v: ' ' },
      { t: 'code', v: 'd' },
      { t: 'text', v: ' ' },
      { t: 'link', href: 'https://x.y', c: [{ t: 'text', v: 'e' }] },
      { t: 'text', v: ' ' },
      { t: 'cite', n: 1 },
      { t: 'cite', n: 2 },
    ]);
    expect(parseInline('[bad](javascript:alert)')).toEqual([{ t: 'text', v: 'bad' }]);
    expect(parseInline('see https://a.b/c.')).toEqual([
      { t: 'text', v: 'see ' },
      { t: 'link', href: 'https://a.b/c', c: [{ t: 'text', v: 'https://a.b/c' }] },
      { t: 'text', v: '.' },
    ]);
    expect(parseInline('snake_case_name')).toEqual([{ t: 'text', v: 'snake_case_name' }]);
  });

  it('builds plain previews and toggles checkboxes', () => {
    expect(plainText('# Shopping\n- [ ] **milk**\n- eggs')).toBe('Shopping milk eggs');
    expect(plainText('x'.repeat(20), 10)).toBe('xxxxxxxxx…');
    expect(toggleCheckbox('- [ ] a\n- [x] b\n- [ ] c', 1)).toBe('- [ ] a\n- [ ] b\n- [ ] c');
    expect(toggleCheckbox('- [ ] a\n- [x] b\n- [ ] c', 2)).toBe('- [ ] a\n- [x] b\n- [x] c');
  });
});
