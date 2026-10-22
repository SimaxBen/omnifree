import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';
import { SHORTCUTS, shortcutIcon, shortcutUrl } from './src/lib/launch';

export default defineConfig({
  plugins: [
    react(),
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
        // Web Push handlers (push, notificationclick, pushsubscriptionchange): a static script from public/,
        // pulled into the generated service worker next to the Workbox precache.
        // Android share target (POST /share-target): public/share-sw.js, same mechanism.
        importScripts: ['push-sw.js', 'share-sw.js'],
        navigateFallback: '/index.html',
        // The hub API is always live data: never serve it from the service worker cache.
        navigateFallbackDenylist: [/^\/(llm|agent|drive|dns|db|plugins|cron|ws|healthz|events|compute|api|notes|tasks|expenses|links|drops|rag|budget|notify|push|search|saved|share-target|s|d)\b/],
        runtimeCaching: [
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
