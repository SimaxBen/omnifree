/* OmniFree Web Push handlers.
 *
 * Loaded into the Workbox service worker with importScripts (vite.config.ts: workbox.importScripts), so the
 * generated precache is untouched. Plain ES2017 script: no imports, no build step.
 *
 * Payload (hub_api/webpush.py build_payload):
 *   { title, body, tag, url, actions: [{ action: 'done' | 'open', title, url }] }
 * 'done' POSTs the signed "Fait ✓" link of a task reminder; 'open' (and a tap on the body) opens `url` in the app.
 *
 * The page keeps { hubUrl, token, publicKey, label, browser } in IndexedDB (omnifree-push / kv / config) so
 * pushsubscriptionchange can re-register a renewed subscription without the page (best effort).
 */
/* eslint-disable no-restricted-globals */
(function (self) {
  'use strict';

  var ICON = '/icons/icon-192.png';
  var BADGE = '/icons/badge-96.png';
  var TASKS = '/#/notes/tasks';
  var DB_NAME = 'omnifree-push';

  function idb(mode, fn) {
    return new Promise(function (resolve, reject) {
      if (!self.indexedDB) return reject(new Error('no IndexedDB'));
      var open = self.indexedDB.open(DB_NAME, 1);
      open.onupgradeneeded = function () {
        open.result.createObjectStore('kv');
      };
      open.onerror = function () {
        reject(open.error);
      };
      open.onsuccess = function () {
        var db = open.result;
        var tx = db.transaction('kv', mode);
        var req = fn(tx.objectStore('kv'));
        tx.oncomplete = function () {
          db.close();
          resolve(req && req.result);
        };
        tx.onerror = function () {
          db.close();
          reject(tx.error);
        };
      };
    });
  }

  function kvGet(key) {
    return idb('readonly', function (s) {
      return s.get(key);
    });
  }

  function kvSet(key, value) {
    return idb('readwrite', function (s) {
      return s.put(value, key);
    });
  }

  /** The payload as an object, whatever the hub sent (JSON, plain text or nothing). */
  function parsePayload(data) {
    if (!data) return { title: 'OmniFree', body: '' };
    try {
      var json = data.json();
      return json && typeof json === 'object' ? json : { title: 'OmniFree', body: String(json) };
    } catch (e) {
      return { title: 'OmniFree', body: data.text() };
    }
  }

  /** Links point at the main PWA origin; on a mirror (pages.dev, netlify.app...) open the same route here. */
  function localUrl(url) {
    var origin = self.location.origin;
    try {
      var u = new URL(url || '/', origin);
      if (u.origin === origin) return u.href;
      if (u.pathname === '/' || u.pathname === '') return origin + '/' + u.search + u.hash;
      return u.href;
    } catch (e) {
      return origin + '/';
    }
  }

  /** showNotification(title, options) arguments for a payload. */
  function notificationFor(p) {
    var actions = Array.isArray(p.actions) ? p.actions.filter(function (a) {
      return a && a.action && a.title;
    }) : [];
    var options = {
      body: p.body || '',
      icon: ICON,
      badge: BADGE,
      data: { url: p.url || '/', actions: actions, tag: p.tag || null },
      actions: actions.slice(0, 2).map(function (a) {
        return { action: String(a.action), title: String(a.title) };
      }),
      timestamp: Date.now(),
    };
    if (p.tag) {
      options.tag = String(p.tag);
      options.renotify = true;
    }
    return { title: p.title || 'OmniFree', options: options };
  }

  function tellClients(message) {
    return self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      list.forEach(function (c) {
        c.postMessage(message);
      });
    });
  }

  function openUrl(url) {
    var target = localUrl(url);
    return self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (new URL(c.url).origin === new URL(target).origin) {
          return c.focus().then(function (focused) {
            var client = focused || c;
            if (client.url === target || !client.navigate) return client;
            return client.navigate(target).catch(function () {
              return client;
            });
          });
        }
      }
      return self.clients.openWindow ? self.clients.openWindow(target) : undefined;
    });
  }

  function markDone(notification, action) {
    return fetch(action.url, { method: 'POST', mode: 'cors', credentials: 'omit' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var tag = (notification.tag || 'omnifree') + '-done';
        var title = String(notification.title || '').replace(/^(❗\s*)?⏰\s*/, '');
        return self.registration
          .showNotification('Fait ✓', { body: title, tag: tag, icon: ICON, badge: BADGE, silent: true, data: { url: TASKS } })
          .then(function () {
            return new Promise(function (resolve) {
              setTimeout(resolve, 4000);
            });
          })
          .then(function () {
            return self.registration.getNotifications({ tag: tag });
          })
          .then(function (list) {
            (list || []).forEach(function (n) {
              n.close();
            });
          });
      })
      .catch(function () {
        return openUrl(TASKS);
      });
  }

  function onPush(event) {
    var payload = parsePayload(event.data);
    var n = notificationFor(payload);
    event.waitUntil(
      Promise.all([
        self.registration.showNotification(n.title, n.options),
        kvSet('lastPush', { at: new Date().toISOString(), tag: payload.tag || null }).catch(function () {}),
        tellClients({ type: 'omnifree-push', tag: payload.tag || null, title: n.title }).catch(function () {}),
      ]),
    );
  }

  function onClick(event) {
    var notification = event.notification;
    var data = notification.data || {};
    var actions = data.actions || [];
    var chosen = null;
    for (var i = 0; i < actions.length; i++) if (actions[i].action === event.action) chosen = actions[i];
    notification.close();
    if (event.action === 'done' && chosen && chosen.url) {
      event.waitUntil(markDone(notification, chosen));
      return;
    }
    event.waitUntil(openUrl((chosen && chosen.url) || data.url || '/'));
  }

  function urlBase64ToUint8Array(base64) {
    var padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    var raw = self.atob(padded);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function onSubscriptionChange(event) {
    event.waitUntil(
      kvGet('config')
        .then(function (cfg) {
          if (!cfg || !cfg.hubUrl || !cfg.token) return;
          var old = event.oldSubscription;
          var key = (old && old.options && old.options.applicationServerKey) ||
            (cfg.publicKey ? urlBase64ToUint8Array(cfg.publicKey) : null);
          var renewed = event.newSubscription
            ? Promise.resolve(event.newSubscription)
            : self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
          var headers = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + cfg.token };
          var base = String(cfg.hubUrl).replace(/\/+$/, '');
          return renewed.then(function (sub) {
            var body = Object.assign(sub.toJSON(), { label: cfg.label || null, browser: cfg.browser || null });
            var jobs = [fetch(base + '/push/subscribe', { method: 'POST', headers: headers, body: JSON.stringify(body) })];
            if (old && old.endpoint && old.endpoint !== sub.endpoint) {
              jobs.push(fetch(base + '/push/subscribe', { method: 'DELETE', headers: headers, body: JSON.stringify({ endpoint: old.endpoint }) }));
            }
            return Promise.all(jobs);
          });
        })
        .catch(function () {
          /* the page re-syncs on its next start */
        }),
    );
  }

  self.addEventListener('push', onPush);
  self.addEventListener('notificationclick', onClick);
  self.addEventListener('pushsubscriptionchange', onSubscriptionChange);

  // Exposed for the unit tests (src/lib/__tests__/push-sw.test.ts).
  self.__omnifreePush = { parsePayload: parsePayload, notificationFor: notificationFor, localUrl: localUrl, urlBase64ToUint8Array: urlBase64ToUint8Array };
})(self);
