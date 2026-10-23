// @vitest-environment jsdom
import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';
import { odtToHtml } from '../preview/odt';
import { sanitizeDocHtml } from '../preview/sanitize';
import { ZipCapError } from '../preview/zip';

const NS =
  'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" ' +
  'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" ' +
  'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink"';

function content(body: string): string {
  return (
    `<?xml version="1.0" encoding="UTF-8"?><office:document-content ${NS}><office:automatic-styles>` +
    '<style:style style:name="B" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>' +
    `</office:automatic-styles><office:body><office:text>${body}</office:text></office:body></office:document-content>`
  );
}

async function odt(body: string, files: Record<string, Uint8Array | string> = {}): Promise<JSZip> {
  const z = new JSZip();
  z.file('mimetype', 'application/vnd.oasis.opendocument.text');
  z.file('content.xml', content(body));
  for (const [k, v] of Object.entries(files)) z.file(k, v);
  return JSZip.loadAsync(await z.generateAsync({ type: 'uint8array', compression: 'DEFLATE' }));
}

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

describe('odt converter', () => {
  it('converts structure and escapes text; the sanitiser strips dangerous links and SVG pictures', async () => {
    const zip = await odt(
      '<text:h text:outline-level="2">Title &lt;script&gt;alert(1)&lt;/script&gt;</text:h>' +
        '<text:p>Hello <text:span text:style-name="B">bold</text:span> ' +
        '<text:a xlink:href="javascript:alert(1)">bad</text:a> <text:a xlink:href="https://ok.example/">good</text:a> ' +
        '<text:a xlink:href="&quot; onclick=&quot;alert(1)">quote</text:a></text:p>' +
        '<text:p><draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame><draw:frame><draw:image xlink:href="Pictures/b.svg"/></draw:frame></text:p>',
      { 'Pictures/a.png': PNG, 'Pictures/b.svg': '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>' },
    );
    const raw = await odtToHtml(zip);
    expect(raw).toContain('<h2>Title &lt;script&gt;alert(1)&lt;/script&gt;</h2>');
    expect(raw).toContain('<strong>bold</strong>');
    const html = sanitizeDocHtml(raw);
    expect(html).not.toMatch(/javascript:|onclick|onload|<script|svg/i);
    expect(html).toContain('<a href="https://ok.example/" target="_blank" rel="noopener noreferrer nofollow">good</a>');
    expect(html).toContain('src="data:image/png;base64,');
  });

  it('refuses a content.xml that inflates past the cap (deflate bomb)', async () => {
    const zip = await odt(`<text:p>${'a'.repeat(50_000)}</text:p>`);
    await expect(odtToHtml(zip, 10_000)).rejects.toBeInstanceOf(ZipCapError);
    await expect(odtToHtml(zip, 100_000)).resolves.toContain('<p>');
  });

  it('rejects a zip without content.xml', async () => {
    const z = new JSZip();
    z.file('x.txt', 'x');
    await expect(odtToHtml(await JSZip.loadAsync(await z.generateAsync({ type: 'uint8array' })))).rejects.toThrow(/OpenDocument/);
  });
});
