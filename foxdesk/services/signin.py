"""Google sign-in for staff (OAuth 2.0 authorization code + PKCE, OpenID Connect).

The Google account's verified email is matched to User.email; there's no
auto-provisioning — someone without a FoxDesk user gets a clear refusal.
Settings are entered on Settings → Sign-in (SigninSettings), falling back to
GOOGLE_OAUTH_CLIENT_ID / _SECRET in the environment.
"""
import base64
import hashlib
import re
import secrets as pysecrets
from urllib.parse import urlencode, urlparse

import requests

from foxdesk.core import ALLOW_SHARED_PASSWORD, APP_URL, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, db
from foxdesk.models import SigninSettings, User
from foxdesk.services.secrets import decrypt_secret, encrypt_secret

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SECRET_PURPOSE = 'google-oauth-secret'
CALLBACK_PATH = '/auth/google/callback'


class SigninError(Exception):
    """Shown to the person trying to sign in."""


def signin_settings():
    row = SigninSettings.query.get(1)
    if row is None:
        row = SigninSettings(id=1, shared_password_disabled=False)
        db.session.add(row)
        db.session.flush()
    return row


def google_config():
    row = SigninSettings.query.get(1)
    saved_secret = decrypt_secret(row.google_client_secret, SECRET_PURPOSE) if row and row.google_client_secret else None
    client_id = (row.google_client_id if row else None) or GOOGLE_OAUTH_CLIENT_ID
    secret = saved_secret or GOOGLE_OAUTH_CLIENT_SECRET
    domains = [d.strip().lower().lstrip('@') for d in re.split(r'[\s,;]+', (row.allowed_domains if row else '') or '') if d.strip()]
    return dict(client_id=client_id, secret=secret, domains=domains,
                configured=bool(client_id and secret),
                sources={'client_id': 'saved' if row and row.google_client_id else ('env' if GOOGLE_OAUTH_CLIENT_ID else None),
                         'secret': 'saved' if saved_secret else ('env' if GOOGLE_OAUTH_CLIENT_SECRET else None)},
                secret_unreadable=bool(row and row.google_client_secret and saved_secret is None))


def save_google_settings(client_id, secret, domains):
    row = signin_settings()
    row.google_client_id = (client_id or '').strip() or None
    if secret:
        row.google_client_secret = encrypt_secret(secret.strip(), SECRET_PURPOSE)
    cleaned = [d.strip().lower().lstrip('@') for d in re.split(r'[\s,;]+', domains or '') if d.strip()]
    if any(not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', d) for d in cleaned):
        raise ValueError('Allowed domains should look like yourdistrict.org, separated by commas.')
    row.allowed_domains = ', '.join(cleaned) or None


def shared_password_allowed():
    if ALLOW_SHARED_PASSWORD:
        return True
    row = SigninSettings.query.get(1)
    return not (row and row.shared_password_disabled)


def redirect_uri(url_root):
    """The callback address to register with Google: APP_URL when set (the
    address people actually use), else what this request came in on."""
    return (APP_URL or url_root.rstrip('/')) + CALLBACK_PATH


def redirect_uri_problem(uri):
    """Why Google would refuse this callback address, or None."""
    parsed = urlparse(uri)
    host = parsed.hostname or ''
    if host in ('localhost', '127.0.0.1') or host.endswith('.localhost'):
        return None
    if parsed.scheme != 'https':
        return 'Google only accepts an https:// address (apart from localhost).'
    if re.fullmatch(r'[\d.]+', host) or host.split('.')[-1] in ('internal', 'local', 'lan', 'home', 'corp', 'intranet', 'test'):
        return (f'Google needs a public domain name (like assets.yourdistrict.org); "{host}" isn\'t one. '
                'It only has to look public: it can still point at this server from inside your network.')
    return None


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def start(session, uri):
    """The Google URL to send the person to; remembers state/nonce/PKCE in the session."""
    cfg = google_config()
    if not cfg['configured']:
        raise SigninError('Google sign-in isn\'t set up yet.')
    state, nonce, verifier = pysecrets.token_urlsafe(24), pysecrets.token_urlsafe(24), pysecrets.token_urlsafe(64)
    session['oauth'] = dict(state=state, nonce=nonce, verifier=verifier, redirect_uri=uri)
    params = dict(client_id=cfg['client_id'], redirect_uri=uri, response_type='code', scope='openid email profile',
                  state=state, nonce=nonce, code_challenge=_b64(hashlib.sha256(verifier.encode()).digest()),
                  code_challenge_method='S256', prompt='select_account')
    if len(cfg['domains']) == 1:
        params['hd'] = cfg['domains'][0]  # a hint to Google's account picker; the domain is checked again below
    return f'{AUTH_URL}?{urlencode(params)}'


def _exchange_code(data):
    return requests.post(TOKEN_URL, data=data, timeout=20)


def _verify_id_token(token, client_id):
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(token, Request(), client_id)


def finish(session, args):
    """Check Google's answer and return the matching active User, or raise SigninError."""
    pending = session.pop('oauth', None)
    if args.get('error'):
        raise SigninError('Google sign-in was cancelled.' if args.get('error') == 'access_denied'
                          else f'Google said: {args.get("error")}.')
    if not pending or not args.get('state') or args.get('state') != pending['state']:
        raise SigninError('That sign-in link expired. Please try again.')
    cfg = google_config()
    try:
        response = _exchange_code(dict(code=args.get('code', ''), client_id=cfg['client_id'], client_secret=cfg['secret'],
                                       redirect_uri=pending['redirect_uri'], grant_type='authorization_code',
                                       code_verifier=pending['verifier']))
    except requests.RequestException:
        raise SigninError('Couldn\'t reach Google. Please try again.')
    if response.status_code != 200 or 'id_token' not in (response.json() or {}):
        raise SigninError('Google didn\'t accept the sign-in. Please try again.')
    try:
        claims = _verify_id_token(response.json()['id_token'], cfg['client_id'])
    except ValueError:
        raise SigninError('Google\'s answer couldn\'t be verified. Please try again.')
    if claims.get('nonce') != pending['nonce']:
        raise SigninError('That sign-in link expired. Please try again.')
    email = (claims.get('email') or '').lower()
    if not email or not claims.get('email_verified'):
        raise SigninError('That Google account has no verified email.')
    if cfg['domains'] and email.split('@')[-1] not in cfg['domains']:
        raise SigninError(f'{email} isn\'t from an allowed domain. Use your school Google account.')
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if not user:
        raise SigninError(f'{email} isn\'t set up in FoxDesk. Ask an administrator to add it to your user.')
    if not user.is_active:
        raise SigninError('Your FoxDesk user is turned off. Ask an administrator.')
    return user
