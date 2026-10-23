import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';
import { SHORTCUTS, shortcutIcon, shortcutUrl } from './src/lib/launch';

// pdf.js support files (wasm image decoders, standard fonts, CMaps) served from this build under
// /pdfjs/, never from a CDN (src/lib/preview/pdfjs.ts). Loaded only when a PDF needs them.
const PDFJS_DIR = join(__dirname, 'node_modules', 'pdfjs-dist');
const PDFJS_ASSETS: Record<string, (name: string) => boolean> = {
  wasm: (n) => /^(openjpeg|jbig2|qcms)[\w.]*\.(wasm|js)$/.test(n) && !n.includes('quickjs'),
  standard_fonts: (n) => !n.startsWith('LICENSE'),
  cmaps: (n) => n.endsWith('.bcmap'),
};

function pdfjsAssets(): Plugin {
  const files = () =>
    Object.entries(PDFJS_ASSETS).flatMap(([dir, keep]) =>
      readdirSync(join(PDFJS_DIR, dir))
        .filter((n) => keep(n) && statSync(join(PDFJS_DIR, dir, n)).isFile())
        .map((n) => `${dir}/${n}`),
    );
  return {
    name: 'omnifree-pdfjs-assets',
    configureServer(server) {
      server.middlewares.use('/pdfjs/', (req, res, next) => {
        const rel = decodeURIComponent((req.url ?? '').split('?')[0]).replace(/^\/+/, '');
        if (!files().includes(rel)) return next();
        res.setHeader('Content-Type', rel.endsWith('.wasm') ? 'application/wasm' : rel.endsWith('.js') ? 'text/javascript' : 'application/octet-stream');
        res.end(readFileSync(join(PDFJS_DIR, rel)));
      });
    },
    generateBundle() {
      for (const rel of files()) this.emitFile({ type: 'asset', fileName: `pdfjs/${rel}`, source: readFileSync(join(PDFJS_DIR, rel)) });
    },
  };
}

// Previewers and their libraries are fetched on first use (runtime cache), not precached at install.
const LAZY_MODULE = /node_modules[\\/](pdfjs-dist|xlsx|mammoth|jszip|highlight\.js|dompurify|@xmldom|bluebird|underscore|lop|dingbat-to-unicode|base64-js|option|pako|lie|readable-stream)[\\/]|[\\/]src[\\/](components[\\/]preview[\\/](PdfViewer|DocPreview|SheetPreview|SlidesPreview|ZipPreview)|lib[\\/]preview[\\/](highlight|pdfjs|sanitize|odt|pptx|zip|ooxml))\./;

export default defineConfig({
  build: {
    rollupOptions: {
      output: {
        chunkFileNames: (chunk) => (!chunk.isEntry && chunk.moduleIds.some((id) => LAZY_MODULE.test(id)) ? 'assets/lazy/[name]-[hash].js' : 'assets/[name]-[hash].js'),
      },
    },
  },
  plugins: [
    react(),
    pdfjsAssets(),
    VitePWA({
      registerType: 'prompt',
      injectRegister: false,
      includeAssets: ['icons/favicon.svg', 'icons/apple-touch-icon.png'],
      manifest: {
        name: 'OmniFree Control',
        short_name: 'OmniFree',
        description: 'Pooled free LLMs, unified drive, DNS, databases and plugin switchboard.',
        theme_color: '#0B0D1E',
        background_color: '#0B0D1E',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        scope: '/',
        icons: [
          { src: 'icons/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' },
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
          { src: 'icons/maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
        // Long press on the app icon (src/lib/launch.ts); icons from scripts/generate-icons.mjs.
        shortcuts: SHORTCUTS.map((s) => ({
          name: s.name,
          short_name: s.short_name,
          description: s.description,
          url: shortcutUrl(s.id),
          icons: [{ src: shortcutIcon(s.id), sizes: '96x96', type: 'image/png' }],
        })),
        // Android share sheet: public/share-sw.js answers this POST in the service worker and opens /#/share.
        share_target: {
          action: '/share-target',
          method: 'POST',
          enctype: 'multipart/form-data',
          params: {
            title: 'title',
            text: 'text',
            url: 'url',
            files: [{ name: 'files', accept: ['image/*', 'application/pdf', '.pdf', 'audio/*'] }],
          },
        },
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // Previewer chunks (pdf.js, SheetJS, mammoth, JSZip, highlight.js) and pdf.js data: runtime-cached below.
        globIgnores: ['**/assets/lazy/**', '**/pdfjs/**'],
        // Web Push handlers (push, notificationclick, pushsubscriptionchange): a static script from public/,
        // pulled into the generated service worker next to the Workbox precache.
        // Android share target (POST /share-target): public/share-sw.js, same mechanism.
        importScripts: ['push-sw.js', 'share-sw.js'],
        navigateFallback: '/index.html',
        // The hub API is always live data: never serve it from the service worker cache.
        navigateFallbackDenylist: [/^\/(llm|agent|drive|dns|db|plugins|cron|ws|healthz|events|compute|api|notes|tasks|expenses|links|drops|rag|budget|notify|push|search|saved|share-target|s|d)\b/],
        runtimeCaching: [
          {
            // Hashed previewer chunks and the pdf.js worker: immutable, kept once used.
            urlPattern: ({ url, sameOrigin }) => sameOrigin && (url.pathname.startsWith('/assets/lazy/') || /^\/assets\/pdf\.worker[\w.-]*\.mjs$/.test(url.pathname)),
            handler: 'CacheFirst',
            options: { cacheName: 'previewers', expiration: { maxEntries: 40, maxAgeSeconds: 60 * 60 * 24 * 90 } },
          },
          {
            urlPattern: ({ url, sameOrigin }) => sameOrigin && url.pathname.startsWith('/pdfjs/'),
            handler: 'CacheFirst',
            options: { cacheName: 'pdfjs-data', expiration: { maxEntries: 120, maxAgeSeconds: 60 * 60 * 24 * 90 } },
          },
          {
            urlPattern: ({ url }) => url.origin === 'https://fonts.googleapis.com' || url.origin === 'https://fonts.gstatic.com',
            handler: 'StaleWhileRevalidate',
            options: { cacheName: 'google-fonts', expiration: { maxEntries: 20, maxAgeSeconds: 60 * 60 * 24 * 365 } },
          },
        ],
      },
    }),
  ],
  server: { port: 5173, host: true },
});
