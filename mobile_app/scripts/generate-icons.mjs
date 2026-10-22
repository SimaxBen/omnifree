// Generates the PWA icons (SVG + PNG) with no dependencies: shapes are rasterised
// from signed-distance functions and encoded with node:zlib.
// Usage: node scripts/generate-icons.mjs
import { deflateSync } from 'node:zlib';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'public', 'icons');
mkdirSync(OUT, { recursive: true });

const BG_TOP = [17, 22, 31];
const BG_BOTTOM = [7, 9, 13];
const SIGNAL = [62, 230, 168];
const RING = [205, 211, 220];

// Geometry in a unit square (0..1). `inset` shrinks the artwork for maskable icons.
function shapes(inset) {
  const s = 1 - inset * 2;
  const p = (v) => inset + v * s;
  const r = (v) => v * s;
  return {
    ring: { cx: p(0.5), cy: p(0.5), r: r(0.27), w: r(0.045) },
    core: { cx: p(0.5), cy: p(0.5), r: r(0.085) },
    nodes: [
      { cx: p(0.5 + 0.27 * Math.cos(-Math.PI / 2)), cy: p(0.5 + 0.27 * Math.sin(-Math.PI / 2)), r: r(0.055) },
      { cx: p(0.5 + 0.27 * Math.cos(Math.PI / 6)), cy: p(0.5 + 0.27 * Math.sin(Math.PI / 6)), r: r(0.055) },
      { cx: p(0.5 + 0.27 * Math.cos((5 * Math.PI) / 6)), cy: p(0.5 + 0.27 * Math.sin((5 * Math.PI) / 6)), r: r(0.055) },
    ],
  };
}

function roundedRectSdf(x, y, radius) {
  const qx = Math.abs(x - 0.5) - (0.5 - radius);
  const qy = Math.abs(y - 0.5) - (0.5 - radius);
  const ox = Math.max(qx, 0);
  const oy = Math.max(qy, 0);
  return Math.hypot(ox, oy) + Math.min(Math.max(qx, qy), 0) - radius;
}

function mix(a, b, t) {
  return a.map((v, i) => v + (b[i] - v) * t);
}

function over(dst, src, alpha) {
  return [mix(dst.slice(0, 3), src, alpha), Math.max(dst[3], alpha)].flat();
}

function sample(x, y, opts) {
  const g = shapes(opts.inset);
  let px = [0, 0, 0, 0];
  if (opts.badge) {
    // Notification badge (Android status bar): white artwork on transparent, only the alpha is used.
    const WHITE = [255, 255, 255];
    const ringB = Math.abs(Math.hypot(x - g.ring.cx, y - g.ring.cy) - g.ring.r) - g.ring.w;
    if (ringB <= 0) px = over(px, WHITE, 1);
    for (const n of g.nodes) {
      if (Math.hypot(x - n.cx, y - n.cy) <= n.r * 1.3) px = over(px, WHITE, 1);
    }
    if (Math.hypot(x - g.core.cx, y - g.core.cy) <= g.core.r * 1.3) px = over(px, WHITE, 1);
    return px;
  }
  const bgInside = opts.fullBleed ? -1 : roundedRectSdf(x, y, 0.22);
  if (bgInside <= 0) px = [...mix(BG_TOP, BG_BOTTOM, y), 1];
  else return px;
  const ringD = Math.abs(Math.hypot(x - g.ring.cx, y - g.ring.cy) - g.ring.r) - g.ring.w / 2;
  if (ringD <= 0) px = over(px, RING, 0.85);
  for (const n of g.nodes) {
    if (Math.hypot(x - n.cx, y - n.cy) <= n.r) px = over(px, SIGNAL, 1);
  }
  if (Math.hypot(x - g.core.cx, y - g.core.cy) <= g.core.r) px = over(px, SIGNAL, 1);
  return px;
}

// App icon shortcut glyphs (manifest.shortcuts): saffron strokes on the night background, drawn on the 24px grid
// of src/components/icons.tsx. Each glyph is polylines, rings and filled discs/capsules.
const SAFFRON = [255, 194, 75];
const NIGHT = [11, 13, 30];

function arc(cx, cy, r, from, to, steps = 24) {
  return Array.from({ length: steps + 1 }, (_, i) => {
    const a = from + ((to - from) * i) / steps;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  });
}

const GLYPHS = {
  voice: { lines: [arc(12, 11, 6.5, 0, Math.PI), [[12, 17.5], [12, 20.5]], [[9, 20.5], [15, 20.5]]], fills: [{ a: [12, 6.5], b: [12, 10.5], r: 3 }] },
  receipt: {
    lines: [
      [[6, 3], [18, 3], [18, 21], [15, 19], [12, 21], [9, 19], [6, 21], [6, 3]],
      [[9, 8], [15, 8]],
      [[9, 12], [15, 12]],
      [[9, 16], [12, 16]],
    ],
  },
  note: { lines: [[[6, 3], [15, 3], [19, 7], [19, 21], [6, 21], [6, 3]], [[14, 3], [14, 8], [19, 8]], [[9, 12], [16, 12]], [[9, 16], [14, 16]]] },
  task: { lines: [arc(12, 12, 8.5, 0, Math.PI * 2, 48), [[8, 12.5], [11, 15.5], [16.5, 9]]] },
  money: {
    lines: [[[3, 6], [21, 6], [21, 18], [3, 18], [3, 6]], arc(12, 12, 3, 0, Math.PI * 2, 32)],
    fills: [{ a: [6.5, 9.5], b: [6.5, 9.5], r: 1 }, { a: [17.5, 14.5], b: [17.5, 14.5], r: 1 }],
  },
};

function segDist(px, py, [ax, ay], [bx, by]) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  const t = len ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len)) : 0;
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
}

function shortcutSample(x, y, glyph) {
  // Glyph area: the middle 60 % of the icon (Android masks shortcut icons to a circle).
  const gx = ((x - 0.2) / 0.6) * 24;
  const gy = ((y - 0.2) / 0.6) * 24;
  let px = [...NIGHT, 1];
  let d = Infinity;
  for (const line of glyph.lines) for (let i = 1; i < line.length; i++) d = Math.min(d, segDist(gx, gy, line[i - 1], line[i]));
  for (const f of glyph.fills ?? []) d = Math.min(d, segDist(gx, gy, f.a, f.b) - f.r + 1);
  if (d <= 1) px = over(px, SAFFRON, 1);
  return px;
}

function render(size, opts) {
  const SS = 4;
  const raw = Buffer.alloc((size * 4 + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0;
    for (let x = 0; x < size; x++) {
      let acc = [0, 0, 0, 0];
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const u = (x + (sx + 0.5) / SS) / size;
          const v = (y + (sy + 0.5) / SS) / size;
          const p = opts.glyph ? shortcutSample(u, v, opts.glyph) : sample(u, v, opts);
          acc = [acc[0] + p[0] * p[3], acc[1] + p[1] * p[3], acc[2] + p[2] * p[3], acc[3] + p[3]];
        }
      }
      const a = acc[3] / (SS * SS);
      const o = y * (size * 4 + 1) + 1 + x * 4;
      raw[o] = a ? Math.round(acc[0] / acc[3]) : 0;
      raw[o + 1] = a ? Math.round(acc[1] / acc[3]) : 0;
      raw[o + 2] = a ? Math.round(acc[2] / acc[3]) : 0;
      raw[o + 3] = Math.round(a * 255);
    }
  }
  return encodePng(size, size, raw);
}

const CRC_TABLE = new Uint32Array(256).map((_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function encodePng(w, h, raw) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function svg(opts) {
  const g = shapes(opts.inset);
  const S = 512;
  const n = (v) => (v * S).toFixed(1);
  const bg = opts.fullBleed
    ? `<rect width="${S}" height="${S}" fill="url(#bg)"/>`
    : `<rect width="${S}" height="${S}" rx="${n(0.22)}" fill="url(#bg)"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${S} ${S}" width="${S}" height="${S}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="rgb(${BG_TOP.join(',')})"/>
      <stop offset="1" stop-color="rgb(${BG_BOTTOM.join(',')})"/>
    </linearGradient>
  </defs>
  ${bg}
  <circle cx="${n(g.ring.cx)}" cy="${n(g.ring.cy)}" r="${n(g.ring.r)}" fill="none" stroke="rgb(${RING.join(',')})" stroke-opacity="0.85" stroke-width="${n(g.ring.w)}"/>
${g.nodes.map((c) => `  <circle cx="${n(c.cx)}" cy="${n(c.cy)}" r="${n(c.r)}" fill="rgb(${SIGNAL.join(',')})"/>`).join('\n')}
  <circle cx="${n(g.core.cx)}" cy="${n(g.core.cy)}" r="${n(g.core.r)}" fill="rgb(${SIGNAL.join(',')})"/>
</svg>
`;
}

const standard = { inset: 0, fullBleed: false };
const maskable = { inset: 0.12, fullBleed: true };

writeFileSync(join(OUT, 'icon.svg'), svg(standard));
writeFileSync(join(OUT, 'favicon.svg'), svg(standard));
writeFileSync(join(OUT, 'maskable.svg'), svg(maskable));
writeFileSync(join(OUT, 'icon-192.png'), render(192, standard));
writeFileSync(join(OUT, 'icon-512.png'), render(512, standard));
writeFileSync(join(OUT, 'maskable-512.png'), render(512, maskable));
writeFileSync(join(OUT, 'apple-touch-icon.png'), render(180, { inset: 0.04, fullBleed: true }));
writeFileSync(join(OUT, 'badge-96.png'), render(96, { inset: 0, fullBleed: false, badge: true }));
for (const [id, glyph] of Object.entries(GLYPHS)) writeFileSync(join(OUT, `shortcut-${id}.png`), render(96, { glyph }));
console.log(`icons written to ${OUT}`);
