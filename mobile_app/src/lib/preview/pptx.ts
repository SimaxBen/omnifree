// PowerPoint (.pptx) as text and images: slides in presentation order (ppt/presentation.xml
// sldIdLst + its rels), each slide's title, paragraphs (with bullet level) and pictures. Not a layout.

import type JSZip from 'jszip';
import { IMAGE_EXT, attrLocal, byLocal, firstLocal, parseXml, readRels, relsPathOf } from './ooxml';

export interface PptxParagraph {
  text: string;
  level: number;
}

export interface PptxSlide {
  /** 1-based position in the deck. */
  number: number;
  part: string;
  title: string;
  paragraphs: PptxParagraph[];
  /** Zip paths of the pictures on the slide (renderable formats only). */
  images: string[];
}

export interface PptxDeck {
  slides: PptxSlide[];
  /** docProps/thumbnail.jpeg (first slide, rendered by PowerPoint) when present. */
  thumbnail: string | null;
}

const MAX_SLIDES = 500;

async function text(zip: JSZip, path: string): Promise<string | null> {
  const f = zip.file(path);
  return f ? f.async('string') : null;
}

function paragraphText(p: Element): string {
  let out = '';
  for (const el of Array.from(p.getElementsByTagName('*'))) {
    if (el.localName === 't') out += el.textContent ?? '';
    else if (el.localName === 'br') out += '\n';
  }
  return out.replace(/[ \t]+/g, ' ').trim();
}

export function parseSlideXml(xml: string, rels: Map<string, { target: string; external: boolean }>): Omit<PptxSlide, 'number' | 'part'> {
  const doc = parseXml(xml);
  let title = '';
  const paragraphs: PptxParagraph[] = [];
  for (const sp of byLocal(doc, 'sp')) {
    const ph = firstLocal(sp, 'ph');
    const phType = ph?.getAttribute('type') ?? '';
    const isTitle = phType === 'title' || phType === 'ctrTitle';
    const body = firstLocal(sp, 'txBody');
    if (!body) continue;
    const paras = byLocal(body, 'p');
    if (isTitle && !title) {
      title = paras.map(paragraphText).filter(Boolean).join(' ');
      continue;
    }
    for (const p of paras) {
      const t = paragraphText(p);
      if (!t) continue;
      const lvl = Number(firstLocal(p, 'pPr')?.getAttribute('lvl') ?? 0);
      paragraphs.push({ text: t, level: Number.isFinite(lvl) ? Math.min(Math.max(lvl, 0), 8) : 0 });
    }
  }
  // Tables (graphicFrame > tbl): one paragraph per row, cells joined.
  for (const tr of byLocal(doc, 'tr')) {
    const cells = byLocal(tr, 'tc').map((tc) => byLocal(tc, 'p').map(paragraphText).filter(Boolean).join(' '));
    if (cells.some(Boolean)) paragraphs.push({ text: cells.join(' | '), level: 0 });
  }
  const images: string[] = [];
  for (const blip of byLocal(doc, 'blip')) {
    const id = attrLocal(blip, 'embed', 'relationships');
    const rel = id ? rels.get(id) : undefined;
    if (rel && !rel.external && IMAGE_EXT.test(rel.target) && !images.includes(rel.target)) images.push(rel.target);
  }
  return { title, paragraphs, images };
}

/** Slide part paths in presentation order. */
export async function slideOrder(zip: JSZip): Promise<string[]> {
  let presentation = 'ppt/presentation.xml';
  const rootRels = await text(zip, '_rels/.rels');
  if (rootRels) {
    for (const r of readRels(rootRels, '').values()) {
      if (r.type.endsWith('/officeDocument')) presentation = r.target;
    }
  }
  const presXml = await text(zip, presentation);
  const presRels = await text(zip, relsPathOf(presentation));
  const order: string[] = [];
  if (presXml && presRels) {
    const rels = readRels(presRels, presentation);
    for (const sld of byLocal(parseXml(presXml), 'sldId')) {
      const id = attrLocal(sld, 'id', 'relationships');
      const rel = id ? rels.get(id) : undefined;
      if (rel && zip.file(rel.target)) order.push(rel.target);
    }
  }
  if (order.length) return order;
  // No usable sldIdLst: slideN.xml by number.
  return Object.keys(zip.files)
    .filter((p) => /^ppt\/slides\/slide\d+\.xml$/.test(p))
    .sort((a, b) => Number(a.match(/(\d+)\.xml$/)![1]) - Number(b.match(/(\d+)\.xml$/)![1]));
}

export async function parsePptx(zip: JSZip): Promise<PptxDeck> {
  if (!zip.file('[Content_Types].xml') || (!zip.file('ppt/presentation.xml') && !zip.file('_rels/.rels'))) {
    throw new Error('This is not a PowerPoint (.pptx) file.');
  }
  const parts = (await slideOrder(zip)).slice(0, MAX_SLIDES);
  const slides: PptxSlide[] = [];
  for (const [i, part] of parts.entries()) {
    const xml = await text(zip, part);
    if (!xml) continue;
    const relsXml = await text(zip, relsPathOf(part));
    const rels = relsXml ? readRels(relsXml, part) : new Map();
    slides.push({ number: i + 1, part, ...parseSlideXml(xml, rels) });
  }
  const thumb = ['docProps/thumbnail.jpeg', 'docProps/thumbnail.jpg', 'docProps/thumbnail.png'].find((p) => zip.file(p));
  return { slides, thumbnail: thumb ?? null };
}
