import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../../components/Markdown';

const html = (text: string, props: Partial<Parameters<typeof Markdown>[0]> = {}) => renderToStaticMarkup(createElement(Markdown, { text, ...props }));

describe('<Markdown> rendering', () => {
  it('renders a scrollable table with a header row', () => {
    const out = html('| A | **B** |\n|---|--:|\n| 1 | 2 |\n| 3 | 4 |');
    expect(out).toContain('overflow-x-auto');
    expect(out).toMatch(/<thead[^>]*><tr><th scope="col"[^>]*><div[^>]*>A<\/div><\/th>/);
    expect(out).toContain('<strong class="font-semibold text-ink-100">B</strong>');
    expect(out).toContain('text-right');
    expect((out.match(/<tr/g) ?? []).length).toBe(3);
    expect(out).toContain('even:bg-ink-900/70');
  });

  it('keeps ordered list numbers and nests lists', () => {
    const out = html('4. four\n5. five\n   - sub');
    expect(out).toMatch(/<ol start="4"/);
    expect(out).toMatch(/<li[^>]*>four<\/li><li[^>]*>five<div[^>]*><ul class="[^"]*list-\[circle\]/);
  });

  it('renders rules, quotes, strike, breaks and h4', () => {
    const out = html('#### Small\n\n---\n\n> quoted ~~old~~\n\nline one\nline two');
    expect(out).toContain('aria-level="4"');
    expect(out).toContain('<hr');
    expect(out).toContain('<blockquote');
    expect(out).toContain('<del class="text-ink-400 decoration-ink-400/70">old</del>');
    expect(out).toContain('line one<br/>line two');
  });

  it('renders code blocks with a language and a copy button', () => {
    const out = html('```bash\necho "<b>hi</b>"\n```');
    expect(out).toContain('>bash</span>');
    expect(out).toContain('aria-label="Copy code"');
    expect(out).toContain('echo &quot;&lt;b&gt;hi&lt;/b&gt;&quot;');
  });

  it('never injects HTML', () => {
    const out = html('<img src=x onerror=alert(1)> **<script>x</script>** [a](javascript:alert(1))');
    expect(out).not.toContain('<img');
    expect(out).not.toContain('<script');
    expect(out).not.toContain('javascript:');
    expect(out).toContain('&lt;img src=x onerror=alert(1)&gt;');
  });

  it('numbers checkboxes in document order for toggling', () => {
    const out = html('- [ ] a\n  - [x] b\n> - [ ] c', { onToggle: () => undefined });
    expect((out.match(/role="checkbox"/g) ?? []).length).toBe(3);
    expect(out.indexOf('aria-label="a"')).toBeLessThan(out.indexOf('aria-label="b"'));
    expect(out).toContain('aria-checked="true" aria-label="b"');
  });

  it('renders citations as buttons only when they can be opened', () => {
    expect(html('See [1].', { onCite: () => undefined })).toContain('aria-label="Source 1"');
    expect(html('See [1].')).toContain('<sup>[1]</sup>');
  });
});
