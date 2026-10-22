import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { HubError } from '../api';
import {
  canonicalUrl,
  flushPendingSaves,
  isRetryableSaveError,
  matchesFilter,
  pendingSaves,
  queuePendingSave,
  type SaveRequest,
} from '../saved';
import { ExampleChip } from '../../views/saved/parts';
import { HOME_RECENT } from '../../views/saved/RecentlySaved';

describe('canonicalUrl keeps the hub dedupe rules', () => {
  it('names an X post by its id only', () => {
    expect(canonicalUrl('https://x.com/i/web/status/20')).toBe('https://x.com/i/status/20');
    expect(canonicalUrl('https://x.com/renamed/status/20?s=46')).toBe('https://x.com/i/status/20');
  });

  it('drops every query parameter on sites whose path names the item', () => {
    expect(canonicalUrl('https://www.tiktok.com/@chef/video/7?is_from_webapp=1&sender_device=pc&_r=1')).toBe('https://tiktok.com/@chef/video/7');
    expect(canonicalUrl('https://www.instagram.com/reel/AbC/?igsh=xyz')).toBe('https://instagram.com/reel/AbC');
    expect(canonicalUrl('https://shop.test/p?color=red')).toBe('https://shop.test/p?color=red');
  });
});

describe('matchesFilter', () => {
  const item = { collection_id: 'c1', type: 'video' as const };
  it('drops an item moved out of the collection being shown', () => {
    expect(matchesFilter(item, { kind: 'collection', id: 'c1' })).toBe(true);
    expect(matchesFilter(item, { kind: 'collection', id: 'c2' })).toBe(false);
    expect(matchesFilter(item, { kind: 'none' })).toBe(false);
    expect(matchesFilter({ ...item, collection_id: null }, { kind: 'none' })).toBe(true);
    expect(matchesFilter(item, { kind: 'type', type: 'video' })).toBe(true);
    expect(matchesFilter(item, { kind: 'all' })).toBe(true);
  });
});

describe('saves queued after "Hide"', () => {
  let store: Map<string, string>;
  const g = globalThis as unknown as { window?: unknown };
  beforeEach(() => {
    store = new Map();
    g.window = {
      localStorage: {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => void store.set(k, v),
        removeItem: (k: string) => void store.delete(k),
      },
    };
  });
  afterEach(() => {
    delete g.window;
  });

  it('only retries what can succeed later', () => {
    expect(isRetryableSaveError(new HubError('Cannot reach hub', 0))).toBe(true);
    expect(isRetryableSaveError(new HubError('Gateway timeout', 504))).toBe(true);
    expect(isRetryableSaveError(new HubError('this link cannot be saved', 422))).toBe(false);
    expect(isRetryableSaveError(new HubError('unauthorised', 401))).toBe(false);
    expect(isRetryableSaveError(new TypeError('Failed to fetch'))).toBe(true);
    expect(isRetryableSaveError(null)).toBe(false);
  });

  it('keeps links and texts (not files), once each', () => {
    queuePendingSave({ url: 'https://a.test/x' });
    queuePendingSave({ url: 'https://a.test/x', note: 'again' });
    queuePendingSave({ text: 'a quote' });
    queuePendingSave({ key: 'saved/files/a.png' });
    expect(pendingSaves().map((p) => p.req)).toEqual([{ url: 'https://a.test/x', note: 'again' }, { text: 'a quote' }]);
  });

  it('sends the queue, keeps retryable failures and drops refused ones', async () => {
    queuePendingSave({ url: 'https://ok.test/' });
    queuePendingSave({ url: 'https://down.test/' });
    queuePendingSave({ url: 'https://refused.test/' });
    const sent: SaveRequest[] = [];
    const n = await flushPendingSaves(async (req) => {
      sent.push(req);
      if (req.url === 'https://down.test/') throw new HubError('Cannot reach hub', 0);
      if (req.url === 'https://refused.test/') throw new HubError('blocked', 422);
      return {};
    });
    expect(n).toBe(1);
    expect(sent.map((r) => r.url)).toEqual(['https://ok.test/', 'https://down.test/', 'https://refused.test/']);
    expect(pendingSaves().map((p) => [p.req.url, p.tries])).toEqual([['https://down.test/', 1]]);
    for (let i = 0; i < 5; i++) await flushPendingSaves(() => Promise.reject(new HubError('Cannot reach hub', 0)));
    expect(pendingSaves()).toEqual([]); // given up after 5 tries
  });
});

describe('<ExampleChip>', () => {
  it('wraps instead of pushing a 375 px page sideways', () => {
    const out = renderToStaticMarkup(createElement(ExampleChip, { text: "C'est quoi l'app pour marcher que j'ai gardée ?", onPick: () => undefined }));
    expect(out).toContain('max-w-full');
    expect(out).not.toContain('shrink-0');
    expect(out).not.toContain('whitespace-nowrap');
  });
});

describe('Home "Recently saved"', () => {
  it('shows at most three items so Home stays short', () => {
    expect(HOME_RECENT).toBe(3);
  });
});
