"""Install FoxDesk as an app: the web app manifest, a service worker, and
the page shown when the network is gone.

The service worker keeps only the offline page and its icon — never a page
with district data on it, since phones and Chromebooks get shared — and
stays out of normal page loads entirely. Browsers only install over HTTPS
(or on localhost).
"""
from flask import Response, jsonify, render_template

from foxdesk.core import app
from foxdesk.services.branding import _current_branding

CACHE_VERSION = 'foxdesk-offline-v2'


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


SERVICE_WORKER = """// FoxDesk service worker: the offline page, and nothing else.
// Pages and files always load straight from the network (the worker doesn't
// touch them), so a browser or network that blocks service-worker requests
// can never break the app; only when the device reports it's offline does a
// page load get the offline page instead of the browser's error.
const CACHE = '%(cache)s';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/offline', '/static/pwa/icon-192.png'])).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate' && self.navigator.onLine === false) {
    event.respondWith(caches.match('/offline'));
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
