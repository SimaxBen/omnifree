import { describe, expect, it } from 'vitest';
import {
  TYPE_ICON,
  canonicalUrl,
  looksLikeUrl,
  saveRequestFromShare,
  savedDate,
  savedFileKey,
  siteDateLine,
  siteOf,
  sortCollections,
  sourceLabel,
  youtubeId,
  type SavedCollection,
} from '../saved';

const share = (p: Partial<{ title: string; text: string; url: string }>) => ({ title: '', text: '', url: '', ...p });

describe('canonicalUrl (same rules as the hub dedupe)', () => {
  it('drops tracking parameters, fragments, www and trailing slashes, and sorts the query', () => {
    expect(canonicalUrl('https://www.Example.com/a/?utm_source=x&b=2&fbclid=y&a=1#frag')).toBe('https://example.com/a?a=1&b=2');
    expect(canonicalUrl('http://example.com:80/')).toBe('https://example.com/');
    expect(canonicalUrl('https://shop.test/p?gclid=1&mc_cid=2&color=red')).toBe('https://shop.test/p?color=red');
  });

  it('folds YouTube, X and Play Store links', () => {
    expect(canonicalUrl('https://youtu.be/abcdefghijk?si=SHARE')).toBe('https://youtube.com/watch?v=abcdefghijk');
    expect(canonicalUrl('https://m.youtube.com/shorts/abcdefghijk?feature=share')).toBe('https://youtube.com/watch?v=abcdefghijk');
    expect(canonicalUrl('https://www.youtube.com/watch?v=abcdefghijk&t=42s')).toBe('https://youtube.com/watch?v=abcdefghijk');
    expect(canonicalUrl('https://twitter.com/Jack/status/20?s=20&t=abc')).toBe('https://x.com/jack/status/20');
    expect(canonicalUrl('https://mobile.x.com/jack/status/20')).toBe('https://x.com/jack/status/20');
    expect(canonicalUrl('https://play.google.com/store/apps/details?id=com.app&hl=fr&gl=MA')).toBe('https://play.google.com/store/apps/details?id=com.app');
    expect(youtubeId('https://youtube.com/embed/abcdefghijk')).toBe('abcdefghijk');
    expect(youtubeId('https://vimeo.com/1')).toBeNull();
  });

  it('refuses anything that is not an http(s) link', () => {
    for (const bad of ['ftp://x.org/f', 'javascript:alert(1)', 'file:///etc/passwd', 'example.com', '']) expect(canonicalUrl(bad)).toBeNull();
    expect(looksLikeUrl(' https://a.test/x ')).toBe(true);
    expect(looksLikeUrl('see https://a.test')).toBe(false);
  });

  it('names the site', () => {
    expect(siteOf('https://www.youtube.com/watch?v=1')).toBe('youtube.com');
    expect(siteOf('https://youtu.be/x')).toBe('youtube.com');
    expect(siteOf('https://twitter.com/a')).toBe('x.com');
    expect(siteOf('https://play.google.com/store')).toBe('play.google.com');
    expect(siteOf(null)).toBe('');
  });
});

describe('saveRequestFromShare (share sheet -> POST /saved)', () => {
  it('saves the link, with a short line as the title hint', () => {
    expect(saveRequestFromShare(share({ text: 'https://x.com/jack/status/20' }))).toEqual({ url: 'https://x.com/jack/status/20' });
    expect(saveRequestFromShare(share({ title: 'Retro anthem', text: 'https://youtu.be/abcdefghijk' }))).toEqual({
      url: 'https://youtu.be/abcdefghijk',
      title: 'Retro anthem',
    });
    expect(saveRequestFromShare(share({ text: 'Engineering managers https://leaddev.test/focus' }))).toEqual({
      url: 'https://leaddev.test/focus',
      title: 'Engineering managers',
    });
    expect(saveRequestFromShare(share({ url: 'https://play.google.com/store/apps/details?id=a', text: 'https://play.google.com/store/apps/details?id=a' }))).toEqual({
      url: 'https://play.google.com/store/apps/details?id=a',
    });
  });

  it('keeps longer shared text as a note', () => {
    const req = saveRequestFromShare(share({ title: 'Article', text: 'First line\nsecond line to remember https://a.test/p' }));
    expect(req).toEqual({ url: 'https://a.test/p', title: 'Article', note: 'First line\nsecond line to remember' });
  });

  it('saves plain text and quotes, and nothing for an empty share', () => {
    expect(saveRequestFromShare(share({ text: '« La simplicité est la sophistication suprême »' }))).toEqual({
      text: '« La simplicité est la sophistication suprême »',
    });
    expect(saveRequestFromShare(share({ title: 'Idée', text: 'Une app pour marcher' }))).toEqual({ text: 'Idée\n\nUne app pour marcher', title: 'Idée' });
    expect(saveRequestFromShare(share({}))).toBeNull();
  });

  it('builds a Drive key for shared files', () => {
    const at = new Date(2026, 9, 22, 10, 0, 0);
    expect(savedFileKey('My photo (1).jpg', at)).toBe(`saved/files/2026/10/${at.getTime()}-My-photo-_1_.jpg`);
    expect(savedFileKey('', at)).toBe(`saved/files/2026/10/${at.getTime()}-file`);
  });
});

describe('site · date lines', () => {
  const now = new Date(2026, 9, 22, 15, 0, 0);
  it('formats the saved date relative to today', () => {
    expect(savedDate(new Date(2026, 9, 22, 8, 0).toISOString(), now)).toBe('Today');
    expect(savedDate(new Date(2026, 9, 21, 23, 0).toISOString(), now)).toBe('Yesterday');
    expect(savedDate(new Date(2026, 9, 3, 12, 0).toISOString(), now)).toBe('3 Oct');
    expect(savedDate(new Date(2025, 11, 31, 12, 0).toISOString(), now)).toBe('31 Dec 2025');
    expect(savedDate('nope', now)).toBe('');
    expect(savedDate(null, now)).toBe('');
  });

  it('names the source and joins it with the date', () => {
    const at = new Date(2026, 9, 21, 9, 0).toISOString();
    expect(siteDateLine({ site: 'youtube.com', kind: 'url', url: 'https://youtu.be/x', saved_at: at }, now)).toBe('youtube.com · Yesterday');
    expect(siteDateLine({ site: '', kind: 'text', url: null, saved_at: at }, now)).toBe('Note · Yesterday');
    expect(sourceLabel({ site: 'Drive', kind: 'file', url: null })).toBe('Drive');
    expect(sourceLabel({ site: '', kind: 'url', url: 'https://www.lemonde.fr/a' })).toBe('lemonde.fr');
    expect(TYPE_ICON['social post']).toBe('chat');
  });

  it('puts collections with items first', () => {
    const c = (id: string, count: number, position: number): SavedCollection => ({ id, name: id, color: null, emoji: null, position, count });
    expect(sortCollections([c('a', 0, 0), c('b', 3, 2), c('c', 1, 1)]).map((x) => x.id)).toEqual(['c', 'b', 'a']);
  });
});
