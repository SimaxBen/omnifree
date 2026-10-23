// Small helpers for Office Open XML / OpenDocument parts read with DOMParser: namespace-agnostic
// element lookups (prefixes differ between producers and in "strict" OOXML) and zip path resolution.

export function parseXml(xml: string): Document {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  if (doc.getElementsByTagName('parsererror').length) throw new Error('Damaged XML inside the file');
  return doc;
}

/** Descendant elements with this local name, in document order. */
export function byLocal(root: Document | Element, name: string): Element[] {
  const all = root.getElementsByTagName('*');
  const out: Element[] = [];
  for (let i = 0; i < all.length; i++) if (all[i].localName === name) out.push(all[i]);
  return out;
}

export function firstLocal(root: Document | Element, name: string): Element | null {
  const all = root.getElementsByTagName('*');
  for (let i = 0; i < all.length; i++) if (all[i].localName === name) return all[i];
  return null;
}

/** Attribute by local name, preferring one in a namespace containing `nsHint` (e.g. r:id over id). */
export function attrLocal(el: Element, name: string, nsHint?: string): string | null {
  let plain: string | null = null;
  for (let i = 0; i < el.attributes.length; i++) {
    const a = el.attributes[i];
    if (a.localName !== name) continue;
    if (!nsHint || (a.namespaceURI ?? '').includes(nsHint)) return a.value;
    plain ??= a.value;
  }
  return nsHint ? plain : null;
}

/** Resolves a relationship target against the folder of the part that owns it. */
export function resolvePart(fromPart: string, target: string): string {
  if (target.startsWith('/')) return target.replace(/^\/+/, '');
  const parts = fromPart.split('/').slice(0, -1);
  for (const seg of target.split('/')) {
    if (seg === '..') parts.pop();
    else if (seg && seg !== '.') parts.push(seg);
  }
  return parts.join('/');
}

/** `_rels/x.xml.rels` of a part. */
export function relsPathOf(part: string): string {
  const i = part.lastIndexOf('/');
  return `${part.slice(0, i + 1)}_rels/${part.slice(i + 1)}.rels`;
}

export interface Relationship {
  id: string;
  type: string;
  target: string;
  external: boolean;
}

export function readRels(xml: string, ownerPart: string): Map<string, Relationship> {
  const out = new Map<string, Relationship>();
  for (const r of byLocal(parseXml(xml), 'Relationship')) {
    const id = r.getAttribute('Id');
    const target = r.getAttribute('Target');
    if (!id || !target) continue;
    const external = r.getAttribute('TargetMode') === 'External';
    out.set(id, { id, type: r.getAttribute('Type') ?? '', target: external ? target : resolvePart(ownerPart, target), external });
  }
  return out;
}

export const IMAGE_EXT = /\.(png|jpe?g|gif|bmp|webp|svg)$/i;

export function imageMime(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  return (
    { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', bmp: 'image/bmp', webp: 'image/webp', svg: 'image/svg+xml' } as Record<string, string>
  )[ext] ?? 'application/octet-stream';
}
