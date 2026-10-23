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

  it('drops classes (no borrowing the app layout) and names (no DOM clobbering), keeps ids for footnotes', () => {
    const out = sanitizeDocHtml('<div class="fixed inset-0 z-50"><a name="x" href="#doc-fn-1" id="doc-ref-1">1</a></div>');
    expect(out).not.toMatch(/class=|name=/);
    expect(out).toContain('id="doc-ref-1"');
    expect(out).toContain('href="#doc-fn-1"');
  });

  it('removes base, meta refresh, link, style, math, templates and data: links', () => {
    const out = sanitizeDocHtml(
      '<base href="https://evil.example/"><meta http-equiv="refresh" content="0;url=https://evil.example"><link rel="stylesheet" href="https://x.example/a.css">' +
        '<style>body{display:none}</style><math><mi>x</mi></math><template><img src=x onerror=alert(1)></template>' +
        `<a href="data:text/html;base64,PHNjcmlwdD4=">d</a><a href="${PNG}">png link</a><a href="vbscript:x">v</a><a href=" JaVaScRiPt:alert(1)">j</a>`,
    );
    expect(out).not.toMatch(/<base|<meta|<link|<style|<math|<template|onerror|data:text|vbscript|javascript/i);
    expect(out).not.toContain(`href="${PNG}"`);
  });

  it('neutralises mutation-XSS shapes', () => {
    const payloads = [
      '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
      '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">',
      '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>',
      '<form><math><mtext></form><form><mglyph><style></math><img src onerror=alert(1)>',
    ];
    for (const p of payloads) {
      const once = sanitizeDocHtml(p);
      // Re-parsing the output (as innerHTML does) must not produce a handler either.
      const twice = sanitizeDocHtml(once);
      expect(once).not.toMatch(/onerror/i);
      expect(twice).not.toMatch(/onerror/i);
    }
  });

  it('keeps the reading structure: headings, emphasis, lists and tables', () => {
    const html = '<h1>T</h1><p><strong>b</strong> <em>i</em></p><ul><li>one</li></ul><ol><li>two</li></ol><table><tbody><tr><td>c</td></tr></tbody></table>';
    expect(sanitizeDocHtml(html)).toBe(html);
  });
});
