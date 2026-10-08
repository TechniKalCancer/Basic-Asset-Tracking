"""Flask app, configuration (from environment variables), extensions, security headers and error pages."""
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, session, url_for
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.security import generate_password_hash


load_dotenv()


logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')


logger = logging.getLogger('asset_tracker')


IS_PRODUCTION = os.environ.get('FLASK_ENV', 'production').lower() == 'production'


DEBUG_MODE = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'


# The package lives one level below the project root, but templates/, static/
# and instance/ stay at the root — point Flask there explicitly.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, root_path=PROJECT_ROOT)


app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL') or 'sqlite:///assets.db'


app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


app.config['SESSION_COOKIE_HTTPONLY'] = True


app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FORCE_HTTPS', 'false').lower() == 'true'


SESSION_TIMEOUT_MINUTES = 180  # how long an idle admin session stays logged in


app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=SESSION_TIMEOUT_MINUTES)


app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB, generous for a CSV upload


app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')


if IS_PRODUCTION:
    if app.secret_key == 'dev-secret-change-in-prod':
        logger.warning('SECURITY WARNING: SECRET_KEY is unset — using the dev fallback in what looks '
                        'like a production environment (FLASK_ENV=production). Set SECRET_KEY in .env.')
    if os.environ.get('ADMIN_PASSWORD') is None:
        logger.warning('SECURITY WARNING: ADMIN_PASSWORD is unset — using the dev fallback password '
                        '("admin123") in what looks like a production environment. Set ADMIN_PASSWORD in .env.')


csrf = CSRFProtect(app)


@app.after_request
def _set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@app.errorhandler(404)
def _handle_not_found(e):
    return render_template('error.html', code=404, message='Page not found.'), 404


@app.errorhandler(413)
def _handle_too_large(e):
    return render_template('error.html', code=413, message='That file is too large to upload.'), 413


@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    """
    A stale/mismatched CSRF token usually just means the form was sitting open
    long enough for the session to change underneath it (timeout, another tab
    logging out, etc.) — redirect back to a fresh login instead of a raw 400.
    """
    session.clear()
    flash('Your session expired before that finished submitting. Please try again.', 'error')
    return redirect(url_for('admin_login'))


@app.errorhandler(500)
def _handle_server_error(e):
    logger.error('Unhandled server error: %s', e)
    return render_template('error.html', code=500, message='Something went wrong on our end.'), 500


WARNING_BEFORE_SECONDS  = 120  # warn 2 min before expiry


ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get('ADMIN_PASSWORD', 'admin123'), method='pbkdf2:sha256'
)


# ─── Google Workspace sync config ──────────────────────────────────────────────
# One service account handles both the read-only info sync and (if opted into
# separately below) the loaner auto-disable write path — its Domain-wide
# Delegation entry in the Workspace Admin console just needs both scopes
# authorized on the same Client ID, not two separate service accounts.
GOOGLE_SERVICE_ACCOUNT_FILE    = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE')


GOOGLE_ADMIN_IMPERSONATE_EMAIL = os.environ.get('GOOGLE_ADMIN_IMPERSONATE_EMAIL')


GOOGLE_SYNC_ENABLED = bool(GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_ADMIN_IMPERSONATE_EMAIL)


GOOGLE_SCOPE_READONLY      = 'https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly'


GOOGLE_SCOPE_MANAGE        = 'https://www.googleapis.com/auth/admin.directory.device.chromeos'


GOOGLE_SCOPE_USER_READONLY = 'https://www.googleapis.com/auth/admin.directory.user.readonly'


GOOGLE_SCOPE_ORGUNIT_READONLY = 'https://www.googleapis.com/auth/admin.directory.orgunit.readonly'


# Remotely disabling a live device is a much bigger blast radius than the
# read-only sync above, so it gets its own separate opt-in on top of
# GOOGLE_SYNC_ENABLED — and a per-site flag on top of that (see Site.google_loaner_autodisable_enabled).
GOOGLE_LOANER_AUTO_DISABLE_ENABLED = os.environ.get('GOOGLE_LOANER_AUTO_DISABLE_ENABLED', '').lower() in ('1', 'true', 'yes')


# ─── KACE SMA sync config ───────────────────────────────────────────────────────
# KACE SMA has no stable public REST API for local console accounts (the
# /ams/shared/api/ JSON endpoint that name suggests is actually unrelated —
# it's for a different subsystem entirely). Reads go through the same
# session-cookie login the admin console itself uses (see _kace_login_session).
KACE_URL         = (os.environ.get('KACE_URL') or '').rstrip('/')


KACE_USERNAME    = os.environ.get('KACE_USERNAME')


KACE_PASSWORD    = os.environ.get('KACE_PASSWORD')


KACE_ORGANIZATION = os.environ.get('KACE_ORGANIZATION', 'Default')


KACE_SYNC_ENABLED = bool(KACE_URL and KACE_USERNAME and KACE_PASSWORD)


# ─── Active Directory sync config ───────────────────────────────────────────────
# Read-only: a plain Domain Users account binds over LDAPS (636) and reads
# users and computers. Nothing is ever written back to AD. A DC with a
# self-signed certificate is trusted from the Active Directory page after an
# admin compares its thumbprint; AD_CA_FILE is for DCs whose certificate comes
# from your own CA (a PEM bundle path inside the container).
AD_SERVERS       = [s.strip() for s in os.environ.get('AD_SERVERS', '').split(',') if s.strip()]
AD_BASE_DN       = os.environ.get('AD_BASE_DN', '').strip()
AD_BIND_USER     = os.environ.get('AD_BIND_USER', '').strip()
AD_BIND_PASSWORD = os.environ.get('AD_BIND_PASSWORD', '')
AD_CA_FILE       = os.environ.get('AD_CA_FILE', '').strip() or None
AD_SYNC_ENABLED  = bool(AD_SERVERS and AD_BASE_DN and AD_BIND_USER and AD_BIND_PASSWORD)


# ─── Email config (Google SMTP by default — smtp.gmail.com with an App Password) ──
# SMTP_USERNAME/SMTP_PASSWORD are optional: a Google Workspace SMTP relay
# (smtp-relay.gmail.com) is commonly set up IP-allowlisted with no login
# required, in which case only SMTP_FROM_EMAIL needs to be set. send_email()
# below only calls server.login() when both are present.
SMTP_SERVER     = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')


SMTP_PORT       = int(os.environ.get('SMTP_PORT', '587'))


SMTP_USERNAME   = os.environ.get('SMTP_USERNAME')


SMTP_PASSWORD   = os.environ.get('SMTP_PASSWORD')


SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL') or SMTP_USERNAME


EMAIL_ENABLED   = bool(SMTP_FROM_EMAIL)


# ─── Branding uploads (logos) ──────────────────────────────────────────────────
# Lives under instance/ (not static/) because it needs to be a writable,
# persistent volume — static/ is baked into the Docker image at build time
# and would lose anything uploaded there on the next deploy. See the
# branding-data volume in docker-compose.yml.
BRANDING_UPLOAD_DIR = os.path.join(app.instance_path, 'branding')


os.makedirs(BRANDING_UPLOAD_DIR, exist_ok=True)


BRANDING_ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico'}


# ─── Simple in-memory login rate limiter ──────────────────────────────────────
_login_attempts = defaultdict(list)  # ip -> [timestamp, ...]


MAX_ATTEMPTS    = 5


LOCKOUT_SECONDS = 300  # 5 minutes


def _check_rate_limit(ip):
    """Returns (allowed, seconds_remaining). Cleans up old attempts."""
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOCKOUT_SECONDS]
    _login_attempts[ip] = attempts
    if len(attempts) >= MAX_ATTEMPTS:
        return False, int(LOCKOUT_SECONDS - (now - attempts[0]))
    return True, 0


def _record_attempt(ip):
    _login_attempts[ip].append(time.time())


db = SQLAlchemy(app)


migrate = Migrate(app, db)
