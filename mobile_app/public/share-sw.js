/* OmniFree Android share target.
 *
 * The web manifest (vite.config.ts: manifest.share_target) makes the installed PWA a target of the Android share
 * sheet: Android POSTs multipart/form-data (title, text, url, files) to /share-target. This script, loaded into the
 * Workbox service worker with importScripts next to push-sw.js, answers that POST itself: it stashes the payload in
 * Cache Storage (cache "omnifree-share": one JSON entry for the texts and file list, one entry per file) and
 * redirects to /#/share, where the Share screen (src/views/ShareScreen.tsx) reads it and asks what to do with it.
 * Nothing leaves the phone until the owner picks an action. Plain ES2017 script: no imports, no build step.
 */
/* eslint-disable no-restricted-globals */
(function (self) {
  'use strict';

  var CACHE = 'omnifree-share';
  var META = '/__share/meta.json';
  var FILE_PREFIX = '/__share/file/';
  var MAX_FILES = 10;
  var MAX_TEXT = 20000;

  function str(value) {
    return typeof value === 'string' ? value.slice(0, MAX_TEXT) : '';
  }

  /** {title, text, url, files: [File]} from the posted form data (empty strings for missing fields). */
  function parseShared(form) {
    var files = [];
    var all = form.getAll ? form.getAll('files') : [];
    for (var i = 0; i < all.length && files.length < MAX_FILES; i++) {
      var f = all[i];
      if (f && typeof f === 'object' && typeof f.size === 'number' && f.size > 0) files.push(f);
    }
    return { title: str(form.get('title')), text: str(form.get('text')), url: str(form.get('url')), files: files };
  }

  /** The JSON stored for the page: texts, receive time and a description of each file (served from the cache). */
  function metaOf(shared, now) {
    return {
      title: shared.title,
      text: shared.text,
      url: shared.url,
      at: new Date(now).toISOString(),
      files: shared.files.map(function (f, i) {
        return { path: FILE_PREFIX + i, name: f.name || 'shared-' + (i + 1), type: f.type || 'application/octet-stream', size: f.size };
      }),
    };
  }

  function stash(shared) {
    var meta = metaOf(shared, Date.now());
    return self.caches.open(CACHE).then(function (cache) {
      return cache
        .keys()
        .then(function (keys) {
          return Promise.all(keys.map(function (k) { return cache.delete(k); }));
        })
        .then(function () {
          var jobs = meta.files.map(function (m, i) {
            return cache.put(m.path, new Response(shared.files[i], { headers: { 'Content-Type': m.type, 'X-Filename': encodeURIComponent(m.name) } }));
          });
          jobs.push(cache.put(META, new Response(JSON.stringify(meta), { headers: { 'Content-Type': 'application/json' } })));
          return Promise.all(jobs);
        });
    });
  }

  function isShareTarget(request) {
    if (request.method !== 'POST') return false;
    try {
      return new URL(request.url).pathname === '/share-target';
    } catch (e) {
      return false;
    }
  }

  function handle(request) {
    return request
      .formData()
      .then(function (form) {
        return stash(parseShared(form));
      })
      .then(function () {
        return Response.redirect(self.location.origin + '/#/share', 303);
      })
      .catch(function () {
        return Response.redirect(self.location.origin + '/#/share?error=1', 303);
      });
  }

  self.addEventListener('fetch', function (event) {
    if (isShareTarget(event.request)) event.respondWith(handle(event.request));
  });

  // Exposed for the unit tests (src/lib/__tests__/share.test.ts).
  self.__omnifreeShare = { parseShared: parseShared, metaOf: metaOf, isShareTarget: isShareTarget, CACHE: CACHE, META: META };
})(self);
