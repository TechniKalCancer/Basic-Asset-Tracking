"""Sign in with Google (faked Google endpoints) and turning off the shared password."""
from urllib.parse import parse_qs, urlparse

from werkzeug.security import generate_password_hash

from conftest import ADMIN_PASSWORD, A, patch_everywhere


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def staff(username='jsmith', email='jsmith@fchs.net', super_admin=False, active=True):
    u = A.User(username=username, email=email, password_hash=generate_password_hash('pw-' + username, method='pbkdf2:sha256'),
               is_super_admin=super_admin, is_active=active, can_devices=True)
    A.db.session.add(u)
    A.db.session.commit()
    return u


def set_up_google(client, domains='fchs.net'):
    r = client.post('/admin/signin/google', data={'client_id': '1234-abc.apps.googleusercontent.com',
                                                  'client_secret': 'GOCSPX-not-real', 'domains': domains})
    assert r.status_code == 302


def google_says(monkeypatch, browser, email, verified=True, nonce=None, state=None, token_status=200):
    """Run the callback as if Google signed `email` in."""
    with browser.session_transaction() as s:
        pending = dict(s.get('oauth') or {})
    claims = {'email': email, 'email_verified': verified, 'nonce': nonce or pending.get('nonce')}
    patch_everywhere(monkeypatch, '_exchange_code', lambda data: FakeResponse(token_status, {'id_token': 'jwt'}))
    patch_everywhere(monkeypatch, '_verify_id_token', lambda token, client_id: claims)
    return browser.get(f'/auth/google/callback?code=abc&state={state or pending.get("state")}', follow_redirects=True)


def test_sign_in_with_google(client, anon_client, monkeypatch):
    set_up_google(client)
    user = staff()
    page = anon_client.get('/admin/login').get_data(as_text=True)
    assert 'Sign in with Google' in page

    r = anon_client.get('/auth/google')
    q = parse_qs(urlparse(r.headers['Location']).query)
    assert r.headers['Location'].startswith('https://accounts.google.com/o/oauth2/v2/auth')
    assert q['client_id'] == ['1234-abc.apps.googleusercontent.com'] and q['code_challenge_method'] == ['S256']
    assert q['hd'] == ['fchs.net'] and q['redirect_uri'][0].endswith('/auth/google/callback')

    r = google_says(monkeypatch, anon_client, 'JSmith@fchs.net')
    with anon_client.session_transaction() as s:
        assert s.get('admin_logged_in') and s.get('user_id') == user.id
    assert b'Admin Login' not in r.data
    assert A.ActivityLog.query.filter(A.ActivityLog.summary.like('%signed in with Google%')).count() == 1


def test_refusals(client, anon_client, monkeypatch):
    set_up_google(client)
    staff()
    staff('gone', 'gone@fchs.net', active=False)
    cases = [
        (dict(email='jsmith@gmail.com'), b'allowed domain'),
        (dict(email='nobody@fchs.net'), b'set up in FoxDesk'),
        (dict(email='gone@fchs.net'), b'turned off'),
        (dict(email='jsmith@fchs.net', verified=False), b'no verified email'),
        (dict(email='jsmith@fchs.net', state='forged'), b'expired'),
        (dict(email='jsmith@fchs.net', nonce='replayed'), b'expired'),
        (dict(email='jsmith@fchs.net', token_status=400), b'didn&#39;t accept'),
    ]
    for kwargs, message in cases:
        anon_client.get('/auth/google')
        r = google_says(monkeypatch, anon_client, **kwargs)
        assert message in r.data, (kwargs, message)
        with anon_client.session_transaction() as s:
            assert not s.get('admin_logged_in')


def test_shared_password_can_be_turned_off(client, anon_client, monkeypatch):
    r = client.post('/admin/signin/shared_password', data={'action': 'off'}, follow_redirects=True)
    assert b'super admin first' in r.data and A.shared_password_allowed()
    staff('boss', 'boss@fchs.net', super_admin=True)
    client.post('/admin/signin/shared_password', data={'action': 'off'})
    assert not A.shared_password_allowed()
    r = anon_client.post('/admin/login', data={'username': '', 'password': ADMIN_PASSWORD}, follow_redirects=True)
    assert b'shared admin password is turned off' in r.data
    r = anon_client.post('/admin/login', data={'username': 'boss', 'password': 'pw-boss'})
    assert r.status_code == 302 and '/admin/login' not in r.headers['Location'], 'own accounts still work'
    patch_everywhere(monkeypatch, 'ALLOW_SHARED_PASSWORD', True)
    assert A.shared_password_allowed(), 'the .env override is the way back in'


def test_settings_page_and_user_emails(client, monkeypatch):
    body = client.get('/admin/signin').get_data(as_text=True)
    assert 'Not set up' in body and '/auth/google/callback' in body
    assert A.redirect_uri_problem('http://assets.fcmiddle.internal/auth/google/callback')
    assert 'public domain' in A.redirect_uri_problem('https://assets.fcmiddle.internal/auth/google/callback')
    assert A.redirect_uri_problem('https://assets.fcmiddle.net/auth/google/callback') is None
    patch_everywhere(monkeypatch, 'APP_URL', 'https://assets.fcmiddle.net')
    assert A.redirect_uri('http://10.16.100.105:8081/') == 'https://assets.fcmiddle.net/auth/google/callback'

    set_up_google(client)
    row = A.SigninSettings.query.get(1)
    assert 'GOCSPX-not-real' not in row.google_client_secret and A.google_config()['secret'] == 'GOCSPX-not-real'
    r = client.post('/admin/signin/google', data={'client_id': 'x', 'domains': 'not a domain'}, follow_redirects=True)
    assert b'should look like' in r.data

    first = staff()
    other = staff('ajones', None)
    r = client.post(f'/admin/users/{other.id}/edit', data={'email': 'JSMITH@fchs.net', 'is_active': 'on'}, follow_redirects=True)
    assert b'already used by' in r.data and A.User.query.get(other.id).email is None
    client.post(f'/admin/users/{other.id}/edit', data={'email': 'AJones@fchs.net', 'is_active': 'on'})
    assert A.User.query.get(other.id).email == 'ajones@fchs.net' and first.email == 'jsmith@fchs.net'


def test_google_button_hidden_until_set_up(anon_client):
    assert b'Sign in with Google' not in anon_client.get('/admin/login').data
    r = anon_client.get('/auth/google', follow_redirects=True)
    assert b'isn&#39;t turned on' in r.data
