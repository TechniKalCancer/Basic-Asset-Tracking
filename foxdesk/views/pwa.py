"""Install FoxDesk as an app: the web app manifest, a service worker, and
the page shown when the network is gone.

The service worker only ever caches the app's own static files (styles,
scripts, icons) and the offline page — never a page with district data on
it, since phones and Chromebooks get shared. Browsers only install over
HTTPS (or on localhost).
"""
from flask import Response, jsonify, render_template

from foxdesk.core import app
from foxdesk.services.branding import _current_branding

CACHE_VERSION = 'foxdesk-static-v1'


@app.route('/manifest.webmanifest')
def pwa_manifest():
    branding = _current_branding()
    name = branding.get('app_name') or 'FoxDesk'
    manifest = {
        'name': name, 'short_name': name[:12], 'id': '/', 'start_url': '/', 'scope': '/',
        'display': 'standalone', 'background_color': '#0f1117', 'theme_color': '#1a1d27',
        'description': 'Devices, loaners, repairs and help desk tickets.',
        'icons': [
            {'src': '/static/pwa/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/pwa/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/pwa/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
        'shortcuts': [
            {'name': 'Look up a device', 'url': '/admin/registry', 'icons': [{'src': '/static/pwa/icon-192.png', 'sizes': '192x192'}]},
            {'name': 'Check in', 'url': '/checkin', 'icons': [{'src': '/static/pwa/icon-192.png', 'sizes': '192x192'}]},
            {'name': 'Tickets', 'url': '/admin/tickets', 'icons': [{'src': '/static/pwa/icon-192.png', 'sizes': '192x192'}]},
        ],
    }
    response = jsonify(manifest)
    response.mimetype = 'application/manifest+json'
    return response


SERVICE_WORKER = """// FoxDesk service worker: static files and the offline page only.
const CACHE = '%(cache)s';
const PRECACHE = ['/offline', '/static/pwa/icon-192.png', '/static/script.js'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate') {
    // Pages always come from the network; the offline page only when there isn't one.
    event.respondWith(fetch(req).catch(() => caches.match('/offline')));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    // Static files: answer from the cache, refresh it in the background.
    event.respondWith(caches.open(CACHE).then(async (cache) => {
      const hit = await cache.match(req);
      const fresh = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; });
      return hit || fresh;
    }));
  }
});
""" % {'cache': CACHE_VERSION}


@app.route('/sw.js')
def pwa_service_worker():
    response = Response(SERVICE_WORKER, mimetype='text/javascript')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@app.route('/offline')
def pwa_offline():
    return render_template('offline.html', app_name=_current_branding().get('app_name') or 'FoxDesk')
