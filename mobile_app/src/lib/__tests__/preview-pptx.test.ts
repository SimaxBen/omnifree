// @vitest-environment jsdom
import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';
import { resolvePart, relsPathOf } from '../preview/ooxml';
import { parsePptx, slideOrder } from '../preview/pptx';

const P = 'http://schemas.openxmlformats.org/presentationml/2006/main';
const A = 'http://schemas.openxmlformats.org/drawingml/2006/main';
const R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const PKG = 'http://schemas.openxmlformats.org/package/2006/relationships';
const SLIDE_T = `${R}/slide`;
const IMAGE_T = `${R}/image`;

function rels(items: [string, string, string][]): string {
  return `<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="${PKG}">${items
    .map(([id, type, target]) => `<Relationship Id="${id}" Type="${type}" Target="${target}"/>`)
    .join('')}</Relationships>`;
}

function para(text: string, lvl?: number): string {
  return `<a:p>${lvl ? `<a:pPr lvl="${lvl}"/>` : ''}<a:r><a:t>${text}</a:t></a:r></a:p>`;
}

function slide(title: string, paras: string[], extra = ''): string {
  return `<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="${P}" xmlns:a="${A}" xmlns:r="${R}"><p:cSld><p:spTree>
  <p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody>${para(title)}</p:txBody></p:sp>
  <p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr><p:txBody>${paras.join('')}</p:txBody></p:sp>
  ${extra}
</p:spTree></p:cSld></p:sld>`;
}

/** A 3-slide deck whose presentation order (sldIdLst) differs from the file numbering. */
async function deck(): Promise<JSZip> {
  const z = new JSZip();
  z.file('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>');
  z.file('_rels/.rels', rels([['rId1', `${R}/officeDocument`, 'ppt/presentation.xml']]));
  z.file(
    'ppt/presentation.xml',
    `<?xml version="1.0"?><p:presentation xmlns:p="${P}" xmlns:r="${R}"><p:sldIdLst>
      <p:sldId id="256" r:id="rId7"/><p:sldId id="257" r:id="rId2"/><p:sldId id="258" r:id="rId5"/>
    </p:sldIdLst></p:presentation>`,
  );
  z.file(
    'ppt/_rels/presentation.xml.rels',
    rels([
      ['rId2', SLIDE_T, 'slides/slide1.xml'],
      ['rId5', SLIDE_T, '/ppt/slides/slide2.xml'],
      ['rId7', SLIDE_T, 'slides/slide3.xml'],
      ['rId9', `${R}/theme`, 'theme/theme1.xml'],
    ]),
  );
  z.file('ppt/slides/slide3.xml', slide('Welcome &amp; agenda', [para('Intro'), para('Budget', 1), para('   ')]));
  z.file(
    'ppt/slides/slide1.xml',
    slide('Numbers', [para('Revenue up')], '<p:pic><p:blipFill><a:blip r:embed="rId3"/></p:blipFill></p:pic><p:pic><p:blipFill><a:blip r:embed="rId4"/></p:blipFill></p:pic>'),
  );
  z.file('ppt/slides/_rels/slide1.xml.rels', rels([['rId3', IMAGE_T, '../media/image1.png'], ['rId4', IMAGE_T, '../media/image2.emf']]));
  z.file(
    'ppt/slides/slide2.xml',
    `<?xml version="1.0"?><p:sld xmlns:p="${P}" xmlns:a="${A}"><p:cSld><p:spTree>
      <p:sp><p:nvSpPr><p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Thank</a:t></a:r><a:r><a:t> you</a:t></a:r></a:p></p:txBody></p:sp>
      <p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody>${para('Q1')}</a:txBody></a:tc><a:tc><a:txBody>${para('10')}</a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>
    </p:spTree></p:cSld></p:sld>`,
  );
  z.file('ppt/media/image1.png', new Uint8Array([0x89, 0x50, 0x4e, 0x47]));
  z.file('docProps/thumbnail.jpeg', new Uint8Array([0xff, 0xd8, 0xff]));
  // Round-trip through bytes, like a real file.
  return JSZip.loadAsync(await z.generateAsync({ type: 'uint8array' }));
}

describe('pptx', () => {
  it('resolves relationship targets', () => {
    expect(resolvePart('ppt/slides/slide1.xml', '../media/a.png')).toBe('ppt/media/a.png');
    expect(resolvePart('ppt/presentation.xml', 'slides/slide1.xml')).toBe('ppt/slides/slide1.xml');
    expect(resolvePart('ppt/presentation.xml', '/ppt/slides/slide2.xml')).toBe('ppt/slides/slide2.xml');
    expect(relsPathOf('ppt/slides/slide1.xml')).toBe('ppt/slides/_rels/slide1.xml.rels');
  });

  it('orders slides by the presentation sldIdLst, not by file name', async () => {
    const z = await deck();
    expect(await slideOrder(z)).toEqual(['ppt/slides/slide3.xml', 'ppt/slides/slide1.xml', 'ppt/slides/slide2.xml']);
  });

  it('extracts titles, bullets with levels, tables and renderable images', async () => {
    const d = await parsePptx(await deck());
    expect(d.thumbnail).toBe('docProps/thumbnail.jpeg');
    expect(d.slides.map((s) => [s.number, s.title])).toEqual([
      [1, 'Welcome & agenda'],
      [2, 'Numbers'],
      [3, 'Thank you'],
    ]);
    expect(d.slides[0].paragraphs).toEqual([
      { text: 'Intro', level: 0 },
      { text: 'Budget', level: 1 },
    ]);
    expect(d.slides[1].paragraphs).toEqual([{ text: 'Revenue up', level: 0 }]);
    expect(d.slides[1].images).toEqual(['ppt/media/image1.png']); // .emf is skipped
    expect(d.slides[2].paragraphs).toEqual([{ text: 'Q1 | 10', level: 0 }]);
  });

  it('falls back to slide numbers when the presentation has no slide list', async () => {
    const z = new JSZip();
    z.file('[Content_Types].xml', '<Types/>');
    z.file('ppt/presentation.xml', `<p:presentation xmlns:p="${P}"/>`);
    z.file('ppt/slides/slide10.xml', slide('Ten', []));
    z.file('ppt/slides/slide2.xml', slide('Two', []));
    const d = await parsePptx(z);
    expect(d.slides.map((s) => s.title)).toEqual(['Two', 'Ten']);
    expect(d.thumbnail).toBeNull();
  });

  it('rejects files that are not presentations', async () => {
    const z = new JSZip();
    z.file('hello.txt', 'hi');
    await expect(parsePptx(z)).rejects.toThrow(/not a PowerPoint/);
  });
});
