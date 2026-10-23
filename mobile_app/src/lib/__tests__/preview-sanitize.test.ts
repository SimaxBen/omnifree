// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { sanitizeDocHtml } from '../preview/sanitize';

const PNG = 'data:image/png;base64,iVBORw0KGgo=';

describe('document HTML sanitiser', () => {
  it('strips scripts, event handlers and frames', () => {
    const out = sanitizeDocHtml(
      '<p onclick="alert(1)" onmouseover="x()">Hi<script>alert(2)</script></p><iframe src="https://evil.example"></iframe><img src=x onerror="alert(3)">',
    );
    expect(out).not.toMatch(/script|onclick|onmouseover|onerror|iframe|alert/i);
    expect(out).toContain('<p>Hi</p>');
  });

  it('drops external pictures, keeps embedded ones', () => {
    const out = sanitizeDocHtml(`<img src="https://tracker.example/pixel.gif"><img src="//cdn.example/a.png"><img src="${PNG}" alt="chart">`);
    expect(out).not.toContain('tracker.example');
    expect(out).not.toContain('cdn.example');
    expect(out).toContain(PNG);
    expect(out.match(/<img/g)).toHaveLength(1);
    expect(sanitizeDocHtml('<img src="data:image/svg+xml;base64,PHN2Zy8+">')).not.toContain('<img');
  });

  it('removes styles, forms, svg and dangerous URLs, and opens links outside the app', () => {
    const out = sanitizeDocHtml(
      '<p style="position:fixed;background:url(https://x.example/a)">t</p><form action="/x"><input name="a"></form><svg><script>1</script></svg>' +
        '<a href="javascript:alert(1)">bad</a><a href="https://ok.example/doc">good</a><a href="#doc-1">note</a>',
    );
    expect(out).not.toMatch(/style=|<form|<input|<svg|javascript:/i);
    expect(out).toContain('<a href="https://ok.example/doc" target="_blank" rel="noopener noreferrer nofollow">good</a>');
    expect(out).toContain('<a href="#doc-1">note</a>');
  });

  it('keeps the reading structure: headings, emphasis, lists and tables', () => {
    const html = '<h1>T</h1><p><strong>b</strong> <em>i</em></p><ul><li>one</li></ul><ol><li>two</li></ol><table><tbody><tr><td>c</td></tr></tbody></table>';
    expect(sanitizeDocHtml(html)).toBe(html);
  });
});
