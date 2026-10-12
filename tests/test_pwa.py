"""Installable app: manifest, service worker, offline page."""
from conftest import A


def test_manifest_service_worker_and_offline_page(anon_client, client):
    m = anon_client.get('/manifest.webmanifest')
    assert m.status_code == 200 and m.mimetype == 'application/manifest+json'
    data = m.get_json()
    assert data['display'] == 'standalone' and data['start_url'] == '/'
    for icon in data['icons']:
        r = anon_client.get(icon['src'])
        assert r.status_code == 200 and r.data[:8] == b'\x89PNG\r\n\x1a\n'
        r.close()

    sw = anon_client.get('/sw.js')
    assert sw.headers['Service-Worker-Allowed'] == '/' and 'javascript' in sw.mimetype
    body = sw.get_data(as_text=True)
    assert "caches.match('/offline')" in body and 'onLine === false' in body, 'only steps in when the device is offline'
    assert "'/admin" not in body and 'cache.put' not in body, 'pages with district data are never cached'

    assert b"You're offline" in anon_client.get('/offline').data
    page = client.get('/admin/tickets').get_data(as_text=True)
    assert 'rel="manifest"' in page and "register('/sw.js')" in page
