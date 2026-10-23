// OpenDocument text (.odt) to simple HTML: headings, paragraphs, bold/italic spans, lists, tables,
// links and embedded pictures (as data: URLs). The result still goes through sanitizeDocHtml.

import type JSZip from 'jszip';
import { IMAGE_EXT, attrLocal, byLocal, firstLocal, imageMime, parseXml } from './ooxml';
import { entryText, extractEntry } from './zip';

const MAX_IMAGE_BYTES = 24 * 1024 * 1024;
/** Inflated size cap of content.xml / styles.xml (a deflate bomb stops here, not at the phone's memory). */
export const ODT_XML_MAX = 48 * 1024 * 1024;

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

interface SpanStyle {
  bold: boolean;
  italic: boolean;
  underline: boolean;
}

function readStyles(doc: Document): Map<string, SpanStyle> {
  const out = new Map<string, SpanStyle>();
  for (const st of byLocal(doc, 'style')) {
    const name = attrLocal(st, 'name', 'style');
    const tp = firstLocal(st, 'text-properties');
    if (!name || !tp) continue;
    out.set(name, {
      bold: attrLocal(tp, 'font-weight') === 'bold',
      italic: attrLocal(tp, 'font-style') === 'italic',
      underline: !!attrLocal(tp, 'text-underline-style') && attrLocal(tp, 'text-underline-style') !== 'none',
    });
  }
  return out;
}

function toBase64(bytes: Uint8Array): string {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export async function odtToHtml(zip: JSZip, xmlMax = ODT_XML_MAX): Promise<string> {
  const content = await entryText(zip, 'content.xml', xmlMax);
  if (content === null) throw new Error('This is not an OpenDocument text (.odt) file.');
  const doc = parseXml(content);
  const styles = readStyles(doc);
  try {
    const stylesXml = await entryText(zip, 'styles.xml', xmlMax);
    if (stylesXml) for (const [k, v] of readStyles(parseXml(stylesXml))) if (!styles.has(k)) styles.set(k, v);
  } catch {
    // Named styles are optional for a preview.
  }

  const images = new Map<string, string>();
  let imageBytes = 0;
  for (const img of byLocal(doc, 'image')) {
    const href = attrLocal(img, 'href', 'xlink') ?? '';
    if (!href || images.has(href) || !IMAGE_EXT.test(href) || /^[a-z]+:/i.test(href)) continue;
    const path = href.replace(/^\.\//, '');
    if (!zip.file(path)) continue;
    let bytes: Uint8Array;
    try {
      bytes = await extractEntry(zip, path, MAX_IMAGE_BYTES - imageBytes);
    } catch {
      break; // the picture budget is spent (or the entry lies about its size)
    }
    imageBytes += bytes.length;
    images.set(href, `data:${imageMime(href)};base64,${toBase64(bytes)}`);
  }

  const inline = (el: Element): string => {
    let out = '';
    for (const node of Array.from(el.childNodes)) {
      if (node.nodeType === 3) {
        out += esc(node.nodeValue ?? '');
        continue;
      }
      if (node.nodeType !== 1) continue;
      const c = node as Element;
      switch (c.localName) {
        case 's':
          out += ' '.repeat(Math.min(Number(attrLocal(c, 'c') ?? 1) || 1, 50));
          break;
        case 'tab':
          out += '\t';
          break;
        case 'line-break':
          out += '<br>';
          break;
        case 'span': {
          const st = styles.get(attrLocal(c, 'style-name', 'text') ?? '');
          let inner = inline(c);
          if (st?.bold) inner = `<strong>${inner}</strong>`;
          if (st?.italic) inner = `<em>${inner}</em>`;
          if (st?.underline) inner = `<u>${inner}</u>`;
          out += inner;
          break;
        }
        case 'a':
          out += `<a href="${esc(attrLocal(c, 'href', 'xlink') ?? '')}">${inline(c)}</a>`;
          break;
        case 'frame':
          out += frame(c);
          break;
        case 'note':
        case 'annotation':
        case 'bookmark':
        case 'bookmark-start':
        case 'bookmark-end':
          break;
        default:
          out += inline(c);
      }
    }
    return out;
  };

  const frame = (el: Element): string => {
    const img = firstLocal(el, 'image');
    const src = img ? images.get(attrLocal(img, 'href', 'xlink') ?? '') : undefined;
    if (src) return `<img src="${src}" alt="">`;
    const box = firstLocal(el, 'text-box');
    return box ? blocks(box) : '';
  };

  const blocks = (el: Element): string => {
    let out = '';
    for (const c of Array.from(el.children)) {
      switch (c.localName) {
        case 'h': {
          const lvl = Math.min(Math.max(Number(attrLocal(c, 'outline-level') ?? 1) || 1, 1), 6);
          out += `<h${lvl}>${inline(c)}</h${lvl}>`;
          break;
        }
        case 'p': {
          const html = inline(c);
          out += html.trim() ? `<p>${html}</p>` : '';
          break;
        }
        case 'list':
          out += `<ul>${Array.from(c.children)
            .filter((li) => li.localName === 'list-item' || li.localName === 'list-header')
            .map((li) => `<li>${blocks(li)}</li>`)
            .join('')}</ul>`;
          break;
        case 'table':
          out += `<table>${byLocal(c, 'table-row')
            .map((tr) => `<tr>${Array.from(tr.children).filter((td) => td.localName === 'table-cell').map((td) => `<td>${blocks(td)}</td>`).join('')}</tr>`)
            .join('')}</table>`;
          break;
        case 'section':
        case 'index-body':
        case 'table-of-content':
          out += blocks(c);
          break;
        case 'frame':
          out += frame(c);
          break;
        default:
      }
    }
    return out;
  };

  const officeBody = firstLocal(doc, 'body');
  const root = officeBody ? (Array.from(officeBody.children).find((c) => c.localName === 'text') ?? officeBody) : null;
  return root ? blocks(root) : '';
}
