"""Running behind Traefik on HTTPS: which forwarded headers are believed, moving
old http addresses to APP_URL, and HSTS."""
from conftest import A, patch_everywhere
from foxdesk.core import _TrustedProxyFix, _parse_networks

APP = 'https://foxdesk.example.org'


def seen_by_app(environ_extra, networks):
    """What the app sees (client IP, scheme) for one request through the middleware."""
    seen = {}

    def app(environ, start_response):
        seen.update(ip=environ['REMOTE_ADDR'], scheme=environ['wsgi.url_scheme'])
        start_response('200 OK', [])
        return [b'']
    environ = {'REQUEST_METHOD': 'GET', 'PATH_INFO': '/', 'SERVER_NAME': 'x', 'SERVER_PORT': '80', 'wsgi.url_scheme': 'http',
               'HTTP_X_FORWARDED_FOR': '10.15.97.20', 'HTTP_X_FORWARDED_PROTO': 'https'}
    environ.update(environ_extra)
    _TrustedProxyFix(app, networks)(environ, lambda *a: None)
    return seen


def test_forwarded_headers_only_from_the_proxy_network():
    nets = _parse_networks('172.23.0.0/16, not-a-network')
    assert len(nets) == 1
    assert seen_by_app({'REMOTE_ADDR': '172.23.0.3'}, nets) == dict(ip='10.15.97.20', scheme='https'), 'Traefik is believed'
    assert seen_by_app({'REMOTE_ADDR': '10.15.40.9'}, nets) == dict(ip='10.15.40.9', scheme='http'), \
        'a direct client claiming another IP or https is not'


def test_old_addresses_move_to_app_url(client, monkeypatch):
    plain = client.get('/admin/people?q=Ruiz')
    assert plain.status_code != 301, 'nothing moves until FORCE_HTTPS and an https APP_URL'
    patch_everywhere(monkeypatch, 'FORCE_HTTPS', True)
    patch_everywhere(monkeypatch, 'APP_URL', APP)

    r = client.get('/admin/people?q=Ruiz')
    assert r.status_code == 301 and r.headers['Location'] == f'{APP}/admin/people?q=Ruiz'
    assert client.get('/r/CB-1').headers['Location'] == f'{APP}/r/CB-1', 'old printed QR labels still land'
    assert client.get('/healthz').status_code == 200, 'the container healthcheck stays on plain http'
    assert client.post('/api/scan', json={}).status_code != 301, 'scripts posting to the old address keep working'
    assert client.post('/admin/login', data={}).status_code != 301, 'a POST is never redirected'

    secure = client.get('/healthz', base_url='https://foxdesk.example.org')
    assert secure.headers['Strict-Transport-Security'] == 'max-age=15552000'
    assert 'Strict-Transport-Security' not in client.get('/healthz').headers
    assert client.get('/admin/login', base_url='https://foxdesk.example.org').status_code == 200


def test_no_hsts_without_force_https(client):
    assert 'Strict-Transport-Security' not in client.get('/healthz', base_url='https://localhost').headers
    assert A.app.config['SESSION_COOKIE_SECURE'] is False
