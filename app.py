from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from sqlalchemy.exc import IntegrityError, ProgrammingError
from datetime import datetime, timezone, timedelta
from functools import wraps
import os
import csv
import io
import time
import threading
import secrets
import smtplib
import logging
import sys
from email.message import EmailMessage
from collections import defaultdict
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('asset_tracker')

IS_PRODUCTION = os.environ.get('FLASK_ENV', 'production').lower() == 'production'
DEBUG_MODE = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL') or 'sqlite:///assets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FORCE_HTTPS', 'false').lower() == 'true'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)
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


SESSION_TIMEOUT_MINUTES = 15
WARNING_BEFORE_SECONDS  = 120  # warn 2 min before expiry

ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get('ADMIN_PASSWORD', 'admin123'), method='pbkdf2:sha256'
)

# ─── Google Workspace sync config (Stage 2 — framework only, not yet implemented) ──
GOOGLE_SERVICE_ACCOUNT_FILE    = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE')
GOOGLE_ADMIN_IMPERSONATE_EMAIL = os.environ.get('GOOGLE_ADMIN_IMPERSONATE_EMAIL')
GOOGLE_SYNC_ENABLED = bool(GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_ADMIN_IMPERSONATE_EMAIL)

# ─── Email config (Google SMTP by default — smtp.gmail.com with an App Password) ──
SMTP_SERVER     = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT       = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USERNAME   = os.environ.get('SMTP_USERNAME')
SMTP_PASSWORD   = os.environ.get('SMTP_PASSWORD')
SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL') or SMTP_USERNAME
EMAIL_ENABLED   = bool(SMTP_USERNAME and SMTP_PASSWORD)

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


# ─── Models ───────────────────────────────────────────────────────────────────

class AssetRegistry(db.Model):
    """
    The source-of-truth list loaded from CSV.
    Each row has an asset_tag and a serial_number.
    Both can be used to look up the same physical item.
    """
    __tablename__ = 'asset_registry'
    id            = db.Column(db.Integer, primary_key=True)
    asset_tag     = db.Column(db.String(120), unique=True, nullable=False, index=True)
    serial_number = db.Column(db.String(120), unique=True, nullable=True, index=True)
    description   = db.Column(db.String(255), nullable=True)
    device_type   = db.Column(db.String(40), nullable=False, default='chromebook', index=True)
    is_loaner     = db.Column(db.Boolean, nullable=False, default=False, index=True)  # part of the short-term loaner pool, not permanently assigned to anyone

    def to_dict(self):
        return {
            'asset_tag': self.asset_tag,
            'serial_number': self.serial_number,
            'description': self.description,
            'device_type': self.device_type,
            'is_loaner': self.is_loaner,
        }


class Asset(db.Model):
    """
    Tracks the current check-in/out status of an asset (keyed by asset_tag).
    is_valid = False means the scan happened before the asset was in the registry;
    it gets healed automatically when a CSV import adds that asset_tag.
    """
    __tablename__ = 'asset'
    id             = db.Column(db.Integer, primary_key=True)
    asset_tag      = db.Column(db.String(120), unique=True, nullable=False, index=True)
    check_in       = db.Column(db.DateTime, nullable=True)
    check_out      = db.Column(db.DateTime, nullable=True)
    is_valid       = db.Column(db.Boolean, default=False, nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True)
    status         = db.Column(db.String(20), nullable=False, default='available')

    # Populated by Google Workspace Chrome device sync (Stage 2, not yet implemented).
    google_model       = db.Column(db.String(120), nullable=True)
    google_org_unit    = db.Column(db.String(255), nullable=True)
    google_recent_user = db.Column(db.String(255), nullable=True)
    google_last_sync_at = db.Column(db.DateTime, nullable=True)

    assigned_to = db.relationship('Person', backref='assets')

    def to_dict(self):
        return {
            'asset_tag':    self.asset_tag,
            'check_in':     self.check_in.isoformat() if self.check_in else None,
            'check_out':    self.check_out.isoformat() if self.check_out else None,
            'is_valid':     self.is_valid,
            'status':       self.status,
            'assigned_to':  self.assigned_to.full_name if self.assigned_to else None,
            'google_model':        self.google_model,
            'google_org_unit':     self.google_org_unit,
            'google_recent_user':  self.google_recent_user,
            'google_last_sync_at': self.google_last_sync_at.isoformat() if self.google_last_sync_at else None,
        }


ASSET_STATUSES = ['available', 'assigned', 'repair', 'lost', 'retired']
DEVICE_TYPES = ['chromebook', 'laptop', 'ipad', 'charger', 'hotspot', 'other']


class Person(db.Model):
    """A staff/student record that an asset can be assigned to."""
    __tablename__ = 'person'
    id         = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name  = db.Column(db.String(80), nullable=False)
    email      = db.Column(db.String(120), unique=True, nullable=False, index=True)
    role       = db.Column(db.String(20), nullable=False, default='staff')  # 'staff' | 'student'
    department = db.Column(db.String(80), nullable=True)
    site       = db.Column(db.String(120), nullable=True, index=True)  # school/building, for disambiguating common names
    external_id = db.Column(db.String(40), unique=True, nullable=True, index=True)  # district staff/student ID — bulk-import upsert key
    grad_year  = db.Column(db.Integer, nullable=True, index=True)  # expected graduation year (students); blank for staff
    is_active  = db.Column(db.Boolean, nullable=False, default=True, index=True)  # False once graduated/withdrawn — keeps history/incidents intact instead of deleting
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    def to_dict(self):
        return {
            'id':          self.id,
            'first_name':  self.first_name,
            'last_name':   self.last_name,
            'email':       self.email,
            'role':        self.role,
            'department':  self.department,
            'site':        self.site,
            'external_id': self.external_id,
            'grad_year':   self.grad_year,
            'is_active':   self.is_active,
        }


class AssignmentHistory(db.Model):
    """
    One row per assignment lifecycle: opened when an asset is assigned to a person,
    closed (unassigned_at set) on unassign or reassignment to someone else.
    person_name is a snapshot taken at assignment time, so history stays readable
    even if the Person record is later deleted.
    """
    __tablename__ = 'assignment_history'
    id             = db.Column(db.Integer, primary_key=True)
    asset_tag      = db.Column(db.String(120), nullable=False, index=True)
    person_id      = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True)
    person_name    = db.Column(db.String(160), nullable=False)
    assigned_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    unassigned_at  = db.Column(db.DateTime, nullable=True)
    due_date       = db.Column(db.Date, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    condition_out  = db.Column(db.String(255), nullable=True)
    condition_in   = db.Column(db.String(255), nullable=True)


class Event(db.Model):
    """
    A check-in/check-out scan. person_name is a snapshot of whoever the asset
    was assigned to at the moment of the scan (blank if unassigned at the time),
    so the log reads "who had it" without needing a live join to Person.
    """
    __tablename__ = 'event'
    id        = db.Column(db.Integer, primary_key=True)
    asset_tag = db.Column(db.String(120), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    action    = db.Column(db.String(50), nullable=False)
    scanned_value = db.Column(db.String(120), nullable=True)   # raw scan (tag or serial)
    scan_type     = db.Column(db.String(20), nullable=True)    # 'asset_tag' | 'serial'
    person_name   = db.Column(db.String(160), nullable=True)

    def to_dict(self):
        return {
            'asset_tag':    self.asset_tag,
            'timestamp':    self.timestamp.isoformat(),
            'action':       self.action,
            'scanned_value': self.scanned_value,
            'scan_type':    self.scan_type,
            'person_name':  self.person_name,
        }


class User(db.Model):
    """
    A named admin account with per-area permissions. Layered on top of the
    single shared ADMIN_PASSWORD login (env var) rather than replacing it —
    that password still logs in as a full superuser, so there's always a
    recovery path if every User account gets locked out or deleted.
    """
    __tablename__ = 'user'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin      = db.Column(db.Boolean, nullable=False, default=False)  # superuser: everything, incl. managing users
    can_people    = db.Column(db.Boolean, nullable=False, default=False)
    can_devices   = db.Column(db.Boolean, nullable=False, default=False)
    can_loaners   = db.Column(db.Boolean, nullable=False, default=False)
    can_repairs   = db.Column(db.Boolean, nullable=False, default=False)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class KioskDevice(db.Model):
    """
    A browser/device enrolled to use Check In / Check Out without admin login.
    Enrollment is done by an admin, from the kiosk device itself, which sets a
    long-lived cookie holding `token`. Revoking here (from any admin session)
    deletes the row, which invalidates that device's cookie immediately.
    """
    __tablename__ = 'kiosk_device'
    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(64), unique=True, nullable=False, index=True)
    label      = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AuditScan(db.Model):
    """
    A physical sighting of an asset during an inventory audit (e.g. walking a
    cart or classroom and scanning every device present). Assets in scope for
    an audit that have no AuditScan since the audit's start date show up as
    "missing" — candidates to track down before they're written off as lost.
    """
    __tablename__ = 'audit_scan'
    id         = db.Column(db.Integer, primary_key=True)
    asset_tag  = db.Column(db.String(120), nullable=False, index=True)
    scanned_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Incident(db.Model):
    """
    A damage/loss report tied to an asset and (usually) whoever had it at the
    time — supports the common school policy of escalating consequences for
    repeat incidents (e.g. 1st free, 2nd billed, 3rd billed + discipline).
    person_name is a snapshot so the record stays meaningful if the person is
    later deleted.
    """
    __tablename__ = 'incident'
    id          = db.Column(db.Integer, primary_key=True)
    asset_tag   = db.Column(db.String(120), nullable=False, index=True)
    person_id   = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True)
    person_name = db.Column(db.String(160), nullable=True)
    description = db.Column(db.Text, nullable=False)
    fee_charged = db.Column(db.Boolean, nullable=False, default=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class LoanerCheckout(db.Model):
    """
    A short-term loaner checkout — unlike AssignmentHistory, a loaner isn't
    pre-assigned to anyone ahead of time, so the student has to identify
    themselves at checkout. person_name is a snapshot, same reasoning as
    elsewhere: history stays readable even if the Person is later deleted.
    """
    __tablename__ = 'loaner_checkout'
    id               = db.Column(db.Integer, primary_key=True)
    asset_tag        = db.Column(db.String(120), nullable=False, index=True)
    person_id        = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True)
    person_name      = db.Column(db.String(160), nullable=False)
    checked_out_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    due_date         = db.Column(db.Date, nullable=True)
    checked_in_at    = db.Column(db.DateTime, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    condition_notes  = db.Column(db.String(255), nullable=True)


with app.app_context():
    try:
        db.create_all()
    except (IntegrityError, ProgrammingError):
        # Multiple gunicorn workers boot concurrently and each run create_all()
        # against a fresh database; the loser of that race hits either a
        # duplicate-key error on Postgres's system catalog (IntegrityError) or
        # a "relation already exists" error for a brand-new table
        # (ProgrammingError) — either way the tables it wanted now exist (the
        # winner just created them). Harmless — move on.
        db.session.rollback()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _generate_asset_tag(existing_tags):
    """
    Returns a random unique 6-digit asset tag (100000-999999) not present in
    existing_tags — used to self-assign a tag when one isn't provided, whether
    adding a device manually or importing a CSV row with no asset_tag/serial.
    """
    for _ in range(50):
        candidate = str(secrets.randbelow(900000) + 100000)
        if candidate not in existing_tags:
            return candidate
    raise RuntimeError('Could not generate a unique asset tag after 50 attempts.')


def resolve_scan(scanned_value: str):
    """
    Given a raw scan value, return (asset_tag, scan_type) or (None, None).
    Checks asset_tag first, then serial_number.
    """
    scanned_value = scanned_value.strip()
    row = AssetRegistry.query.filter_by(asset_tag=scanned_value).first()
    if row:
        return row.asset_tag, 'asset_tag'
    row = AssetRegistry.query.filter_by(serial_number=scanned_value).first()
    if row:
        return row.asset_tag, 'serial'
    return None, None


def heal_orphans():
    """
    After a CSV import, mark previously-invalid Asset records as valid
    if their asset_tag now exists in the registry.
    """
    orphans = Asset.query.filter_by(is_valid=False).all()
    healed = 0
    for asset in orphans:
        if AssetRegistry.query.filter_by(asset_tag=asset.asset_tag).first():
            asset.is_valid = True
            healed += 1
    if healed:
        db.session.commit()
    return healed


def sync_chromeos_device_from_google(serial_number):
    """
    Looks up a Chromebook by serial number via the Google Admin SDK Directory API
    and returns its model, org unit, and most recently synced user.

    Stage 2 work — not implemented yet. Requires a Google Cloud service account
    with domain-wide delegation authorized (in the Workspace Admin console) for
    the https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly
    scope, impersonating a super admin (GOOGLE_ADMIN_IMPERSONATE_EMAIL).

    Args:
        serial_number: The device's manufacturer serial number.

    Returns:
        A dict with keys 'model', 'org_unit', 'recent_user'.

    Raises:
        NotImplementedError: Always, until Stage 2 is built out.
    """
    from google.oauth2 import service_account  # noqa: F401 (Stage 2 wiring)
    from googleapiclient.discovery import build  # noqa: F401 (Stage 2 wiring)
    raise NotImplementedError('Google Workspace sync is configured but not implemented yet.')


def send_email(to_email, subject, body):
    """
    Sends a plain-text email via SMTP (Gmail by default: smtp.gmail.com:587 with
    an App Password — a regular account password will not work with 2FA enabled).

    Args:
        to_email: Recipient address.
        subject: Email subject line.
        body: Plain-text email body.

    Raises:
        RuntimeError: If SMTP_USERNAME/SMTP_PASSWORD are not configured.
        smtplib.SMTPException, OSError: On connection/authentication/send failure.
    """
    if not EMAIL_ENABLED:
        raise RuntimeError('Email is not configured (set SMTP_USERNAME and SMTP_PASSWORD).')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM_EMAIL
    msg['To'] = to_email
    msg.set_content(body)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


def _admin_session_active():
    """
    True if there's a currently valid (non-expired) admin session. Clears an
    expired session as a side effect, and refreshes the sliding timeout when valid.
    """
    if not session.get('admin_logged_in'):
        return False
    last_active = session.get('last_active')
    if last_active:
        elapsed = datetime.now(timezone.utc).timestamp() - last_active
        if elapsed > SESSION_TIMEOUT_MINUTES * 60:
            # Only drop the auth keys, not the whole session — a full clear()
            # also wipes the CSRF token, which silently invalidates any login
            # form already open in another tab (or from an earlier redirect
            # here) even though that page's token was never actually used yet.
            session.pop('admin_logged_in', None)
            session.pop('last_active', None)
            return False
    session['last_active'] = datetime.now(timezone.utc).timestamp()
    session.permanent = True
    return True


def _kiosk_device_valid():
    """True if the request carries a cookie token matching an enrolled KioskDevice."""
    token = request.cookies.get('kiosk_token')
    return bool(token) and KioskDevice.query.filter_by(token=token).first() is not None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        was_logged_in = session.get('admin_logged_in')
        if not _admin_session_active():
            msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                   else 'Please log in to access the admin panel.')
            flash(msg, 'error')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def _current_user():
    """The logged-in User row, or None if this session used the legacy shared
    ADMIN_PASSWORD login (which has no associated User record)."""
    user_id = session.get('user_id')
    return User.query.get(user_id) if user_id else None


def _has_permission(perm):
    """
    session['is_admin'] is set for both the legacy ADMIN_PASSWORD login and any
    User with is_admin=True — either way, a superuser passes every check.
    Otherwise perm must match one of the current User's can_* columns.
    """
    if session.get('is_admin'):
        return True
    user = _current_user()
    if not user or not user.is_active:
        return False
    return {
        'people':  user.can_people,
        'devices': user.can_devices,
        'loaners': user.can_loaners,
        'repairs': user.can_repairs,
    }.get(perm, False)


def require_permission(perm):
    """Like login_required, but also requires the given area permission
    ('people', 'devices', 'loaners', 'repairs', or 'admin' for superuser-only)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            was_logged_in = session.get('admin_logged_in')
            if not _admin_session_active():
                msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                       else 'Please log in to access the admin panel.')
                flash(msg, 'error')
                return redirect(url_for('admin_login'))
            if not _has_permission(perm):
                flash('Your account doesn\'t have permission to access that page.', 'error')
                return redirect(url_for('admin_panel'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def kiosk_or_login_required(f):
    """Allows either an active admin session or an enrolled kiosk device's cookie."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _admin_session_active() or _kiosk_device_valid():
            return f(*args, **kwargs)
        flash('Please log in, or use a device enrolled in Kiosk Mode.', 'error')
        return redirect(url_for('admin_login'))
    return decorated


def api_login_required(f):
    """Like login_required, but returns a JSON 401 instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _admin_session_active():
            return jsonify({'error': 'Unauthorized. Admin login required.'}), 401
        return f(*args, **kwargs)
    return decorated


def kiosk_or_api_login_required(f):
    """Like kiosk_or_login_required, but returns a JSON 401 instead of redirecting."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _admin_session_active() or _kiosk_device_valid():
            return f(*args, **kwargs)
        return jsonify({'error': 'Unauthorized. Log in or use a device enrolled in Kiosk Mode.'}), 401
    return decorated


# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.context_processor
def inject_permission_helper():
    """Exposes can('people'|'devices'|'loaners'|'repairs') to every template, so
    nav links and buttons can hide themselves for users without that permission
    instead of just bouncing them back with an error after they click."""
    return {'can': _has_permission}


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Redirect already-logged-in admins
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_panel'))

    if request.method == 'POST':
        ip = request.remote_addr
        allowed, wait = _check_rate_limit(ip)

        if not allowed:
            flash(f'Too many failed attempts. Try again in {wait} seconds.', 'error')
            return render_template('admin_login.html')

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Blank username = the legacy shared ADMIN_PASSWORD login, always a
        # full superuser. A username looks up a named User account instead.
        if not username:
            if check_password_hash(ADMIN_PASSWORD_HASH, password):
                session.clear()
                session['admin_logged_in'] = True
                session['is_admin'] = True
                session['last_active'] = datetime.now(timezone.utc).timestamp()
                session.permanent = True
                return redirect(url_for('admin_panel'))
        else:
            user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
            if user and user.is_active and check_password_hash(user.password_hash, password):
                session.clear()
                session['admin_logged_in'] = True
                session['user_id'] = user.id
                session['is_admin'] = user.is_admin
                session['last_active'] = datetime.now(timezone.utc).timestamp()
                session.permanent = True
                return redirect(url_for('admin_panel'))

        _record_attempt(ip)
        attempts_left = MAX_ATTEMPTS - len(_login_attempts[ip])
        flash(f'Invalid username or password. {attempts_left} attempt{"s" if attempts_left != 1 else ""} remaining.', 'error')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin_login'))


@app.route('/api/admin/session_status')
def session_status():
    """Returns seconds remaining in session — used by the timeout warning UI."""
    if not session.get('admin_logged_in'):
        return jsonify({'authenticated': False})
    last_active = session.get('last_active', 0)
    elapsed = datetime.now(timezone.utc).timestamp() - last_active
    remaining = max(0, SESSION_TIMEOUT_MINUTES * 60 - int(elapsed))
    return jsonify({'authenticated': True, 'seconds_remaining': remaining})


# ─── Admin Panel ──────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_panel():
    registry_count = AssetRegistry.query.count()
    orphan_count   = Asset.query.filter_by(is_valid=False).count()
    people_count   = Person.query.count()

    # Assets with no Asset row yet are implicitly 'available' (the default status).
    explicit_counts = dict(
        db.session.query(Asset.status, db.func.count(Asset.id)).group_by(Asset.status).all()
    )
    non_available_explicit = sum(v for k, v in explicit_counts.items() if k != 'available')
    status_counts = {s: explicit_counts.get(s, 0) for s in ASSET_STATUSES}
    status_counts['available'] = registry_count - non_available_explicit
    overdue_count = len(_overdue_assignments())

    return render_template('admin_panel.html',
                           registry_count=registry_count,
                           orphan_count=orphan_count,
                           people_count=people_count,
                           status_counts=status_counts,
                           overdue_count=overdue_count,
                           email_enabled=EMAIL_ENABLED,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/upload_csv', methods=['POST'])
@require_permission('admin')
def upload_csv():
    """
    Accepts a CSV with columns: asset_tag, serial_number (optional), description (optional).
    Completely replaces the registry. Heals any orphaned Asset records afterwards.
    """
    if 'csv_file' not in request.files:
        flash('No file part in request', 'error')
        return redirect(url_for('admin_panel'))

    file = request.files['csv_file']
    if not file.filename.lower().endswith('.csv'):
        flash('File must be a .csv', 'error')
        return redirect(url_for('admin_panel'))

    try:
        content = file.stream.read().decode('utf-8-sig')  # strips BOM

        # Auto-detect delimiter from first line
        first_line = content.splitlines()[0] if content.splitlines() else ''
        delimiter = '\t' if '\t' in first_line else ','

        # Read raw headers from first line and normalize them
        raw_headers = next(csv.reader([first_line], delimiter=delimiter))
        normalized_headers = [h.strip().lower().replace(' ', '_') for h in raw_headers]

        # Feed remaining content to DictReader with normalized headers
        stream = io.StringIO(content)
        reader = csv.DictReader(stream, delimiter=delimiter)
        reader.fieldnames = normalized_headers
        next(reader)  # skip the original header row

        # Accept MDM column names "asset_id" / "asset id" as well as "asset_tag"
        TAG_COLS    = ('asset_id', 'asset_tag')
        SERIAL_COLS = ('serial_number',)
        DESC_COLS   = ('description',)
        TYPE_COLS   = ('device_type', 'type')

        tag_col    = next((c for c in TAG_COLS    if c in normalized_headers), None)
        serial_col = next((c for c in SERIAL_COLS if c in normalized_headers), None)
        desc_col   = next((c for c in DESC_COLS   if c in normalized_headers), None)
        type_col   = next((c for c in TYPE_COLS   if c in normalized_headers), None)

        if not any((tag_col, serial_col, desc_col, type_col)):
            flash(f'CSV must have at least one recognized column (asset_tag, serial_number, '
                  f'description, or device_type). Found: {", ".join(normalized_headers)}', 'error')
            return redirect(url_for('admin_panel'))

        rows = list(reader)

        # Wipe old registry and replace
        AssetRegistry.query.delete()
        db.session.flush()

        imported = 0
        skipped  = 0
        auto_assigned = 0
        seen_tags    = set()
        seen_serials = set()

        def clean(val):
            """Return None if value is empty, '0', or whitespace."""
            v = (val or '').strip()
            return None if (not v or v == '0') else v

        for row in rows:
            tag    = clean(row.get(tag_col, ''))    if tag_col    else None
            serial = clean(row.get(serial_col, '')) if serial_col else None
            desc   = clean(row.get(desc_col, ''))   if desc_col   else None
            dtype  = clean(row.get(type_col, ''))   if type_col   else None
            dtype  = dtype.lower() if dtype and dtype.lower() in DEVICE_TYPES else 'chromebook'

            # If asset_id is missing but serial exists, use serial as the tag
            if not tag and serial:
                tag = serial

            # Skip rows where every recognized column is blank (e.g. stray blank CSV lines)
            if not tag and not serial and not desc:
                skipped += 1
                continue

            # Still no tag (no asset_id/serial given) but the row has real data — self-assign one
            if not tag:
                tag = _generate_asset_tag(seen_tags)
                auto_assigned += 1

            # Skip duplicate tags
            if tag in seen_tags:
                skipped += 1
                continue

            # Drop duplicate serial but keep the tag
            if serial and serial in seen_serials:
                serial = None

            seen_tags.add(tag)
            if serial:
                seen_serials.add(serial)

            db.session.add(AssetRegistry(
                asset_tag=tag,
                serial_number=serial,
                description=desc,
                device_type=dtype,
            ))
            imported += 1

        db.session.commit()
        healed = heal_orphans()

        msg = f'Imported {imported} assets.'
        if auto_assigned:
            msg += f' Self-assigned a tag for {auto_assigned} row{"s" if auto_assigned != 1 else ""} with none given.'
        if skipped:
            msg += f' Skipped {skipped} duplicate/invalid rows.'
        if healed:
            msg += f' Healed {healed} previously-unknown asset record(s).'
        flash(msg, 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Import failed: {e}', 'error')

    return redirect(url_for('admin_panel'))


def _filter_registry_by_status(query, status_filter):
    """Filters an AssetRegistry query by live Asset.status. Assets with no Asset
    row yet are implicitly 'available' (the default), so that case is handled
    by excluding tags with any explicit non-available status rather than requiring one."""
    if status_filter == 'available':
        non_available = db.session.query(Asset.asset_tag).filter(Asset.status != 'available')
        return query.filter(~AssetRegistry.asset_tag.in_(non_available))
    matching = db.session.query(Asset.asset_tag).filter(Asset.status == status_filter)
    return query.filter(AssetRegistry.asset_tag.in_(matching))


@app.route('/admin/registry/new', methods=['GET', 'POST'])
@require_permission('devices')
def admin_registry_new():
    """Manually adds a single device. Leave asset_tag blank to self-assign a random 6-digit one."""
    if request.method == 'POST':
        tag = request.form.get('asset_tag', '').strip()
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'

        if tag and AssetRegistry.query.filter_by(asset_tag=tag).first():
            flash(f'Asset tag "{tag}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form)

        if serial and AssetRegistry.query.filter_by(serial_number=serial).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form)

        if not tag:
            existing_tags = {t for (t,) in db.session.query(AssetRegistry.asset_tag).all()}
            tag = _generate_asset_tag(existing_tags)

        try:
            db.session.add(AssetRegistry(
                asset_tag=tag, serial_number=serial,
                description=description, device_type=device_type,
            ))
            db.session.commit()
            flash(f'Added device {tag} to the registry.', 'success')
            return redirect(url_for('admin_asset_assign', asset_tag=tag))
        except IntegrityError as e:
            db.session.rollback()
            flash('Could not add device: that asset tag or serial number is already in use.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form)
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add device: {e}', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form)

    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=None)


@app.route('/admin/registry')
@require_permission('devices')
def admin_registry():
    page          = request.args.get('page', 1, type=int)
    per_page      = 50
    query         = AssetRegistry.query.order_by(AssetRegistry.asset_tag)
    search        = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    type_filter   = request.args.get('device_type', '').strip()
    person_filter = request.args.get('person_id', '').strip()

    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                AssetRegistry.asset_tag.ilike(like),
                AssetRegistry.serial_number.ilike(like),
                AssetRegistry.description.ilike(like),
            )
        )
    if status_filter in ASSET_STATUSES:
        query = _filter_registry_by_status(query, status_filter)
    else:
        status_filter = ''
    if type_filter in DEVICE_TYPES:
        query = query.filter(AssetRegistry.device_type == type_filter)
    else:
        type_filter = ''

    person_filter_name = None
    if person_filter.isdigit():
        person = Person.query.get(int(person_filter))
        if person:
            person_filter_name = person.full_name
            owned_tags = db.session.query(Asset.asset_tag).filter(Asset.assigned_to_id == person.id)
            query = query.filter(AssetRegistry.asset_tag.in_(owned_tags))
        else:
            person_filter = ''
    else:
        person_filter = ''

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    page_tags = [row.asset_tag for row in pagination.items]
    assets_by_tag = {
        a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(page_tags))
    }

    return render_template('admin_registry.html', pagination=pagination, search=search,
                           status_filter=status_filter, asset_statuses=ASSET_STATUSES,
                           type_filter=type_filter, device_types=DEVICE_TYPES,
                           person_filter=person_filter, person_filter_name=person_filter_name,
                           assets_by_tag=assets_by_tag)


@app.route('/admin/registry/export')
@require_permission('devices')
def admin_registry_export():
    """Exports the full asset list (not just the current page) as CSV, including
    live status and assignment — useful as a backup/reporting snapshot."""
    rows = AssetRegistry.query.order_by(AssetRegistry.asset_tag).all()
    assets_by_tag = {a.asset_tag: a for a in Asset.query.all()}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['asset_tag', 'serial_number', 'description', 'device_type', 'status', 'assigned_to', 'assigned_to_email'])
    for row in rows:
        asset = assets_by_tag.get(row.asset_tag)
        status = asset.status if asset else 'available'
        person = asset.assigned_to if asset else None
        writer.writerow([
            row.asset_tag, row.serial_number or '', row.description or '', row.device_type,
            status, person.full_name if person else '', person.email if person else '',
        ])

    response = app.response_class(buffer.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=asset_export.csv'
    return response


@app.route('/admin/scan_lookup')
@require_permission('devices')
def admin_scan_lookup():
    """
    Jumps straight to an asset's assign page from a scanned/typed tag or serial number.
    Works with any USB barcode scanner, since those just type into the focused
    field and send Enter — no special hardware integration needed.
    """
    value = request.args.get('value', '').strip()
    if not value:
        return redirect(url_for('admin_registry'))

    asset_tag, _ = resolve_scan(value)
    if not asset_tag:
        flash(f'No asset found matching "{value}".', 'error')
        return redirect(url_for('admin_registry'))

    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/search')
@login_required
def admin_search():
    """
    Global lookup from the nav bar search box — checks both People and Assets
    at once. A single unambiguous match jumps straight to that record instead
    of showing a results page.
    """
    q = request.args.get('q', '').strip()
    people, assets = [], []

    if len(q) >= 2:
        people = Person.query.filter(_person_search_filter(q)) \
            .order_by(Person.last_name, Person.first_name).limit(25).all()

        like = f'%{q}%'
        registry_rows = AssetRegistry.query.filter(
            db.or_(
                AssetRegistry.asset_tag.ilike(like),
                AssetRegistry.serial_number.ilike(like),
                AssetRegistry.description.ilike(like),
            )
        ).order_by(AssetRegistry.asset_tag).limit(25).all()
        tags = [r.asset_tag for r in registry_rows]
        assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(tags))}
        assets = [(r, assets_by_tag.get(r.asset_tag)) for r in registry_rows]

        if len(people) == 1 and not assets:
            return redirect(url_for('admin_registry', person_id=people[0].id))
        if len(assets) == 1 and not people:
            return redirect(url_for('admin_asset_assign', asset_tag=assets[0][0].asset_tag))

    return render_template('admin_search.html', q=q, people=people, assets=assets)


@app.route('/admin/orphans')
@require_permission('devices')
def admin_orphans():
    orphans = Asset.query.filter_by(is_valid=False).order_by(Asset.asset_tag).all()
    return render_template('admin_orphans.html', orphans=orphans)


# ─── People ───────────────────────────────────────────────────────────────────

def _person_search_filter(q):
    """
    Multi-token fuzzy match: each whitespace-separated token must match at least
    one field. This lets "John Smith" (or "Smith John") find John Smith even
    though neither single field contains the whole two-word query.
    """
    conditions = []
    for token in q.split():
        like = f'%{token}%'
        conditions.append(db.or_(
            Person.first_name.ilike(like),
            Person.last_name.ilike(like),
            Person.email.ilike(like),
            Person.site.ilike(like),
            Person.external_id.ilike(like),
        ))
    return db.and_(*conditions)


@app.route('/admin/people')
@require_permission('people')
def admin_people():
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    show     = request.args.get('show', 'active')  # 'active' | 'inactive' | 'all'
    query    = Person.query.order_by(Person.last_name, Person.first_name)
    if show == 'active':
        query = query.filter(Person.is_active.is_(True))
    elif show == 'inactive':
        query = query.filter(Person.is_active.is_(False))
    search   = request.args.get('q', '').strip()
    if search:
        query = query.filter(_person_search_filter(search))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('admin_people.html', pagination=pagination, search=search, show=show)


@app.route('/admin/people/search')
@api_login_required
def admin_people_search():
    """
    Live search for the person-picker widget (e.g. on the assign page) — returns
    a small JSON list of matches instead of ever loading the full roster client-side,
    so this stays fast with thousands of people. Only active people are returned,
    so a graduated/withdrawn person can't accidentally be assigned a device.
    """
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])

    matches = Person.query.filter(
        Person.is_active.is_(True),
        _person_search_filter(q),
    ).order_by(Person.last_name, Person.first_name).limit(20).all()

    return jsonify([{
        'id': p.id, 'full_name': p.full_name, 'email': p.email, 'site': p.site,
    } for p in matches])


def _person_form_values():
    """Reads and normalizes the People create/edit form fields from the request."""
    grad_year_raw = request.form.get('grad_year', '').strip()
    return {
        'first_name':  request.form.get('first_name', '').strip(),
        'last_name':   request.form.get('last_name', '').strip(),
        'email':       request.form.get('email', '').strip().lower(),
        'role':        request.form.get('role', 'staff').strip(),
        'department':  request.form.get('department', '').strip() or None,
        'site':        request.form.get('site', '').strip() or None,
        'external_id': request.form.get('external_id', '').strip() or None,
        'grad_year':   int(grad_year_raw) if grad_year_raw.isdigit() else None,
    }


def _validate_person_form(values, person_id=None):
    """Returns an error message string, or None if the form values are valid."""
    if not values['first_name'] or not values['last_name'] or not values['email']:
        return 'First name, last name, and email are required.'
    if '@' not in values['email'] or '.' not in values['email'].split('@')[-1]:
        return 'Enter a valid email address.'
    if values['external_id']:
        dupe = Person.query.filter(Person.external_id == values['external_id'])
        if person_id:
            dupe = dupe.filter(Person.id != person_id)
        if dupe.first():
            return f'ID number "{values["external_id"]}" is already assigned to another person.'
    return None


@app.route('/admin/people/new', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_new():
    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=None, form=values)

        try:
            person = Person(**values)
            db.session.add(person)
            db.session.commit()
            flash(f'Added {person.full_name}.', 'success')
            return redirect(url_for('admin_people'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add person: {e}', 'error')
            return render_template('admin_person_form.html', person=None, form=values)

    return render_template('admin_person_form.html', person=None, form=None)


@app.route('/admin/people/<int:person_id>/edit', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_edit(person_id):
    person = Person.query.get_or_404(person_id)

    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values, person_id=person_id)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=person, form=values)

        try:
            for field, value in values.items():
                setattr(person, field, value)
            db.session.commit()
            flash(f'Updated {person.full_name}.', 'success')
            return redirect(url_for('admin_people'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not update person: {e}', 'error')
            return render_template('admin_person_form.html', person=person, form=values)

    return render_template('admin_person_form.html', person=person, form=None)


def _release_person_assets(person, condition_in):
    """Unassigns every asset currently held by a person. Shared by delete and
    the bulk graduate action. Returns the number of assets released."""
    affected_assets = Asset.query.filter_by(assigned_to_id=person.id).all()
    for asset in affected_assets:
        _close_open_assignment(asset.asset_tag, condition_in=condition_in)
        asset.assigned_to_id = None
        asset.status = 'available'
    return len(affected_assets)


@app.route('/admin/people/<int:person_id>/delete', methods=['POST'])
@require_permission('people')
def admin_person_delete(person_id):
    """
    Permanently deletes a person record. Any assets currently assigned to them
    are unassigned first, not blocked. AssignmentHistory/Incident rows are kept
    (person_name is a snapshot) but their person_id link is cleared so the
    foreign key doesn't block the delete.

    For students leaving at graduation, prefer /admin/people/graduate instead —
    it archives (is_active=False) rather than deleting, so history/incidents
    stay fully linked. Use this route for genuine data-entry mistakes.
    """
    person = Person.query.get_or_404(person_id)
    try:
        unassigned = _release_person_assets(person, condition_in='Person deleted')
        AssignmentHistory.query.filter_by(person_id=person.id).update({'person_id': None})
        Incident.query.filter_by(person_id=person.id).update({'person_id': None})
        db.session.delete(person)
        db.session.commit()
        msg = f'Deleted {person.full_name}.'
        if unassigned:
            msg += f' Unassigned {unassigned} asset{"s" if unassigned != 1 else ""}.'
        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete person: {e}', 'error')
    return redirect(url_for('admin_people'))


@app.route('/admin/people/<int:person_id>/reactivate', methods=['POST'])
@require_permission('people')
def admin_person_reactivate(person_id):
    """Undoes an accidental graduate/archive — marks a person active again."""
    person = Person.query.get_or_404(person_id)
    person.is_active = True
    db.session.commit()
    flash(f'Reactivated {person.full_name}.', 'success')
    return redirect(url_for('admin_people', show='inactive'))


@app.route('/admin/people/import', methods=['GET', 'POST'])
@require_permission('people')
def admin_people_import():
    """
    Bulk create-or-update people from a district roster CSV — the "everyone
    gets an ID number" workflow. Matches each row to an existing person by
    external_id first, falling back to email (for rows/people that don't have
    an ID number yet); if neither matches, a new person is created. Unlike the
    asset registry import, this never wipes existing rows — it's an upsert.
    """
    results = None

    if request.method == 'POST':
        if 'csv_file' not in request.files or not request.files['csv_file'].filename:
            flash('Choose a CSV file to upload.', 'error')
            return redirect(url_for('admin_people_import'))

        file = request.files['csv_file']
        if not file.filename.lower().endswith('.csv'):
            flash('File must be a .csv', 'error')
            return redirect(url_for('admin_people_import'))

        results = []
        try:
            content = file.stream.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
            fieldnames = [(f or '').strip().lower().replace(' ', '_') for f in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames

            if 'first_name' not in fieldnames or 'last_name' not in fieldnames or 'email' not in fieldnames:
                flash(f'CSV must have "first_name", "last_name", and "email" columns. '
                      f'Found: {", ".join(fieldnames)}', 'error')
                return redirect(url_for('admin_people_import'))

            def clean(val):
                v = (val or '').strip()
                return v or None

            created = updated = skipped = 0
            for row in reader:
                first_name = clean(row.get('first_name'))
                last_name  = clean(row.get('last_name'))
                email      = clean(row.get('email'))
                email      = email.lower() if email else None
                external_id = clean(row.get('external_id') or row.get('staff_id') or row.get('student_id'))
                role       = clean(row.get('role'))
                role       = role.lower() if role and role.lower() in ('staff', 'student') else None
                department = clean(row.get('department'))
                site       = clean(row.get('site'))
                grad_year_raw = clean(row.get('grad_year') or row.get('graduation_year'))
                grad_year  = int(grad_year_raw) if grad_year_raw and grad_year_raw.isdigit() else None

                if not first_name or not last_name or not email:
                    skipped += 1
                    results.append({'row': email or external_id or '(blank)', 'ok': False,
                                    'message': 'Missing first_name, last_name, or email.'})
                    continue

                person = None
                if external_id:
                    person = Person.query.filter_by(external_id=external_id).first()
                if not person:
                    person = Person.query.filter(db.func.lower(Person.email) == email).first()

                if person and external_id and person.external_id and person.external_id != external_id:
                    skipped += 1
                    results.append({'row': email, 'ok': False,
                                    'message': f'ID number conflict: {email} already has ID {person.external_id}.'})
                    continue

                if person:
                    person.first_name = first_name
                    person.last_name  = last_name
                    person.email      = email
                    if external_id:  person.external_id = external_id
                    if role:         person.role = role
                    if department:   person.department = department
                    if site:         person.site = site
                    if grad_year:    person.grad_year = grad_year
                    updated += 1
                    results.append({'row': email, 'ok': True, 'message': f'Updated {person.full_name}.'})
                else:
                    person = Person(
                        first_name=first_name, last_name=last_name, email=email,
                        external_id=external_id, role=role or 'staff',
                        department=department, site=site, grad_year=grad_year,
                    )
                    db.session.add(person)
                    created += 1
                    results.append({'row': email, 'ok': True, 'message': f'Created {first_name} {last_name}.'})

            db.session.commit()
            flash(f'Created {created}, updated {updated}, skipped {skipped} row(s). See details below.',
                  'success' if not skipped else 'info')

        except Exception as e:
            db.session.rollback()
            flash(f'Import failed: {e}', 'error')
            return redirect(url_for('admin_people_import'))

    return render_template('admin_people_import.html', results=results)


@app.route('/admin/people/graduate', methods=['GET', 'POST'])
@require_permission('people')
def admin_people_graduate():
    """
    Bulk-removes a graduating class from the active roster in one action.
    Archives (is_active=False) rather than deletes, so assignment history and
    incident/fee records stay intact for anyone who ever had a device — it
    just stops them from showing up in People or the assign-device search.
    """
    if request.method == 'POST':
        grad_year_raw = request.form.get('grad_year', '').strip()
        if not grad_year_raw.isdigit():
            flash('Choose a valid graduation year.', 'error')
            return redirect(url_for('admin_people_graduate'))
        grad_year = int(grad_year_raw)

        students = Person.query.filter_by(role='student', grad_year=grad_year, is_active=True).all()
        if not students:
            flash(f'No active students found with graduation year {grad_year}.', 'info')
            return redirect(url_for('admin_people_graduate'))

        unassigned_total = 0
        for student in students:
            unassigned_total += _release_person_assets(student, condition_in='Graduated')
            student.is_active = False
        db.session.commit()

        flash(f'Graduated {len(students)} student{"s" if len(students) != 1 else ""} '
              f'(class of {grad_year}). Unassigned {unassigned_total} device'
              f'{"s" if unassigned_total != 1 else ""}.', 'success')
        return redirect(url_for('admin_people', show='inactive'))

    grad_year_counts = dict(
        db.session.query(Person.grad_year, db.func.count(Person.id))
        .filter(Person.role == 'student', Person.is_active.is_(True), Person.grad_year.isnot(None))
        .group_by(Person.grad_year).order_by(Person.grad_year).all()
    )
    return render_template('admin_people_graduate.html', grad_year_counts=grad_year_counts)


# ─── Asset Assignment ─────────────────────────────────────────────────────────

def _close_open_assignment(asset_tag, condition_in=None):
    """Closes the current open AssignmentHistory row for an asset_tag, if any."""
    open_row = AssignmentHistory.query.filter_by(asset_tag=asset_tag, unassigned_at=None).first()
    if open_row:
        open_row.unassigned_at = datetime.utcnow()
        open_row.condition_in = condition_in


def _assign_asset_to_person(asset_tag, person, condition_out=None, due_date=None):
    """
    Shared assign logic used by both the single assign form and bulk assign.
    The asset_tag must already exist in the registry (caller's responsibility
    to check) — the live Asset row is created here if scanning hasn't made one yet.

    Returns:
        (status, message) where status is 'assigned', 'already', or 'error'.
    """
    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    if asset and asset.assigned_to_id == person.id:
        return 'already', f'{asset_tag} is already assigned to {person.full_name}.'

    try:
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        _close_open_assignment(asset_tag, condition_in='Reassigned')
        db.session.add(AssignmentHistory(
            asset_tag=asset_tag, person_id=person.id, person_name=person.full_name,
            condition_out=condition_out, due_date=due_date,
        ))
        asset.assigned_to_id = person.id
        asset.status = 'assigned'
        db.session.commit()
        return 'assigned', f'Assigned {asset_tag} to {person.full_name}.'
    except Exception as e:
        db.session.rollback()
        return 'error', f'Could not assign {asset_tag}: {e}'


@app.route('/admin/assets/<string:asset_tag>/assign', methods=['GET', 'POST'])
@require_permission('devices')
def admin_asset_assign(asset_tag):
    """
    Assigns a person to an asset_tag. The asset_tag must exist in the registry;
    the live Asset row is created on first assignment if scanning hasn't made one yet.
    Reassigning to someone new closes out the prior AssignmentHistory row and opens
    a new one; reassigning to the same person is a no-op.
    """
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first_or_404()
    asset = Asset.query.filter_by(asset_tag=asset_tag).first()

    if request.method == 'POST':
        person_id = request.form.get('person_id', type=int)
        condition_out = request.form.get('condition_out', '').strip() or None
        due_date_str = request.form.get('due_date', '').strip()
        if not person_id:
            flash('Select a person to assign.', 'error')
            return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid due date.', 'error')
                return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

        person = Person.query.get_or_404(person_id)
        status, message = _assign_asset_to_person(asset_tag, person, condition_out, due_date)
        flash(message, 'info' if status == 'already' else ('success' if status == 'assigned' else 'error'))
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    has_people = Person.query.first() is not None
    history = AssignmentHistory.query.filter_by(asset_tag=asset_tag) \
        .order_by(AssignmentHistory.assigned_at.desc()).all()
    events = Event.query.filter_by(asset_tag=asset_tag) \
        .order_by(Event.timestamp.desc()).limit(20).all()
    incidents = Incident.query.filter_by(asset_tag=asset_tag) \
        .order_by(Incident.created_at.desc()).all()

    current_person_incident_count = None
    if asset and asset.assigned_to_id:
        current_person_incident_count = Incident.query.filter_by(person_id=asset.assigned_to_id).count()

    return render_template('admin_assign.html', registry_row=registry_row, asset=asset, has_people=has_people,
                           history=history, events=events, incidents=incidents,
                           current_person_incident_count=current_person_incident_count,
                           asset_statuses=ASSET_STATUSES,
                           now=datetime.utcnow().date(), google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/bulk_assign', methods=['GET', 'POST'])
@require_permission('admin')
def admin_bulk_assign():
    """
    Bulk-assigns a whole roster in one upload — the start-of-year "hand out
    every Chromebook" workflow. CSV columns: asset_tag, email, due_date (optional).
    People must already exist (use /admin/people or import them first); this
    intentionally does not auto-create people from a typo'd email.
    """
    results = None

    if request.method == 'POST':
        if 'csv_file' not in request.files or not request.files['csv_file'].filename:
            flash('Choose a CSV file to upload.', 'error')
            return redirect(url_for('admin_bulk_assign'))

        file = request.files['csv_file']
        if not file.filename.lower().endswith('.csv'):
            flash('File must be a .csv', 'error')
            return redirect(url_for('admin_bulk_assign'))

        results = []
        try:
            content = file.stream.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
            fieldnames = [(f or '').strip().lower() for f in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames

            if 'asset_tag' not in fieldnames or 'email' not in fieldnames:
                flash(f'CSV must have "asset_tag" and "email" columns. Found: {", ".join(fieldnames)}', 'error')
                return redirect(url_for('admin_bulk_assign'))

            for row in reader:
                asset_tag = (row.get('asset_tag') or '').strip()
                email = (row.get('email') or '').strip().lower()
                due_date_str = (row.get('due_date') or '').strip()

                if not asset_tag or not email:
                    results.append({'asset_tag': asset_tag or '(blank)', 'email': email, 'ok': False,
                                    'message': 'Missing asset_tag or email.'})
                    continue

                if not AssetRegistry.query.filter_by(asset_tag=asset_tag).first():
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'Asset tag not found in registry.'})
                    continue

                person = Person.query.filter(db.func.lower(Person.email) == email).first()
                if not person:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'No person with this email — add them first.'})
                    continue
                if not person.is_active:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': f'{person.full_name} is graduated/inactive — reactivate first.'})
                    continue

                due_date = None
                if due_date_str:
                    try:
                        due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                        'message': f'Invalid due_date "{due_date_str}" (use YYYY-MM-DD).'})
                        continue

                status, message = _assign_asset_to_person(asset_tag, person, due_date=due_date)
                results.append({'asset_tag': asset_tag, 'email': email, 'ok': status != 'error', 'message': message})

        except Exception as e:
            flash(f'Bulk assign failed: {e}', 'error')
            return redirect(url_for('admin_bulk_assign'))

        succeeded = sum(1 for r in results if r['ok'])
        flash(f'Assigned {succeeded} of {len(results)} row(s). See details below.',
              'success' if succeeded == len(results) else 'info')

    return render_template('admin_bulk_assign.html', results=results)


@app.route('/admin/bulk_print')
@require_permission('devices')
def admin_bulk_print():
    """
    Lists devices to print labels for (defaults to currently-assigned ones —
    the "just handed out a cart of Chromebooks" case) with checkboxes; actual
    printing happens client-side via the DYMO SDK, looping over the selection
    in the same order the rows appear in the table.

    order=scan lists devices in the order they were scanned during an Asset
    Audit session instead of alphabetical by tag — so labels print in the same
    sequence they were physically handled, and can be applied stack-by-stack
    without hunting back through everything already set aside.
    """
    type_filter = request.args.get('device_type', '').strip()
    order_mode = request.args.get('order', 'tag').strip()
    if order_mode not in ('tag', 'scan'):
        order_mode = 'tag'
    default_status = '' if order_mode == 'scan' else 'assigned'
    status_filter = request.args.get('status', default_status).strip()

    since_str = request.args.get('since', '').strip()
    try:
        since = datetime.strptime(since_str, '%Y-%m-%d') if since_str else None
    except ValueError:
        since = None
    if not since:
        since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    since_str = since.strftime('%Y-%m-%d')

    if type_filter not in DEVICE_TYPES:
        type_filter = ''
    if status_filter not in ASSET_STATUSES:
        status_filter = ''

    if order_mode == 'scan':
        # First-scan time per tag today (or since the given date) gives the
        # exact physical walk order; re-scanning a tag doesn't move it later.
        scan_times = (db.session.query(AuditScan.asset_tag, db.func.min(AuditScan.scanned_at))
                      .filter(AuditScan.scanned_at >= since)
                      .group_by(AuditScan.asset_tag)
                      .order_by(db.func.min(AuditScan.scanned_at))
                      .all())
        ordered_tags = [tag for tag, _ in scan_times]
        registry_by_tag = {r.asset_tag: r for r in AssetRegistry.query.filter(
            AssetRegistry.asset_tag.in_(ordered_tags)
        )}
        rows = [registry_by_tag[t] for t in ordered_tags if t in registry_by_tag]
        if type_filter:
            rows = [r for r in rows if r.device_type == type_filter]
    else:
        query = AssetRegistry.query.order_by(AssetRegistry.asset_tag)
        if type_filter:
            query = query.filter(AssetRegistry.device_type == type_filter)
        rows = query.all()

    assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(
        Asset.asset_tag.in_([r.asset_tag for r in rows])
    )}

    if status_filter:
        def _matches_status(row):
            asset = assets_by_tag.get(row.asset_tag)
            current = asset.status if asset else 'available'
            return current == status_filter
        rows = [r for r in rows if _matches_status(r)]

    candidates = []
    for row in rows:
        asset = assets_by_tag.get(row.asset_tag)
        person = asset.assigned_to if asset else None
        candidates.append({'asset_tag': row.asset_tag, 'person_name': person.full_name if person else ''})

    return render_template('admin_bulk_print.html', candidates=candidates,
                           status_filter=status_filter, type_filter=type_filter,
                           order_mode=order_mode, since=since_str,
                           asset_statuses=ASSET_STATUSES, device_types=DEVICE_TYPES)


@app.route('/admin/assets/<string:asset_tag>/unassign', methods=['POST'])
@require_permission('devices')
def admin_asset_unassign(asset_tag):
    asset = Asset.query.filter_by(asset_tag=asset_tag).first_or_404()
    condition_in = request.form.get('condition_in', '').strip() or None
    try:
        _close_open_assignment(asset_tag, condition_in=condition_in)
        asset.assigned_to_id = None
        asset.status = 'available'
        db.session.commit()
        flash(f'Unassigned {asset_tag}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not unassign asset: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/assets/<string:asset_tag>/status', methods=['POST'])
@require_permission('devices')
def admin_asset_status(asset_tag):
    """Manual status override, independent of assignment (e.g. marking a device 'repair')."""
    AssetRegistry.query.filter_by(asset_tag=asset_tag).first_or_404()
    new_status = request.form.get('status', '')
    if new_status not in ASSET_STATUSES:
        flash('Invalid status.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    try:
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.status = new_status
        db.session.commit()
        flash(f'{asset_tag} status set to {new_status}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update status: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/assets/<string:asset_tag>/google_sync', methods=['POST'])
@require_permission('devices')
def admin_asset_google_sync(asset_tag):
    """Pulls model/org-unit/recent-user from Google Workspace for one asset, by serial number."""
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first_or_404()

    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet. Set GOOGLE_SERVICE_ACCOUNT_FILE '
              'and GOOGLE_ADMIN_IMPERSONATE_EMAIL in .env to enable it.', 'info')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    if not registry_row.serial_number:
        flash(f'{asset_tag} has no serial number on file to look up.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    try:
        info = sync_chromeos_device_from_google(registry_row.serial_number)
        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.google_model        = info.get('model')
        asset.google_org_unit     = info.get('org_unit')
        asset.google_recent_user  = info.get('recent_user')
        asset.google_last_sync_at = datetime.utcnow()
        db.session.commit()
        flash(f'Synced {asset_tag} from Google.', 'success')
    except NotImplementedError as e:
        flash(str(e), 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Google sync failed: {e}', 'error')

    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


# ─── Kiosk Devices ──────────────────────────────────────────────────────────────

@app.route('/admin/kiosk')
@require_permission('admin')
def admin_kiosk():
    devices = KioskDevice.query.order_by(KioskDevice.created_at.desc()).all()
    token = request.cookies.get('kiosk_token')
    current_device = KioskDevice.query.filter_by(token=token).first() if token else None
    return render_template('admin_kiosk.html', devices=devices, current_device=current_device)


@app.route('/admin/kiosk/enable', methods=['POST'])
@require_permission('admin')
def admin_kiosk_enable():
    """Enrolls the device making this request (i.e. the kiosk itself) via a long-lived cookie."""
    label = request.form.get('label', '').strip() or None
    token = secrets.token_urlsafe(32)
    try:
        db.session.add(KioskDevice(token=token, label=label))
        db.session.commit()
        resp = redirect(url_for('admin_kiosk'))
        resp.set_cookie('kiosk_token', token, max_age=60 * 60 * 24 * 365 * 5,
                        httponly=True, samesite='Lax')
        flash('This device is now enrolled as a kiosk — Check In/Check Out will work here without logging in.', 'success')
        return resp
    except Exception as e:
        db.session.rollback()
        flash(f'Could not enroll device: {e}', 'error')
        return redirect(url_for('admin_kiosk'))


@app.route('/admin/kiosk/<int:device_id>/revoke', methods=['POST'])
@require_permission('admin')
def admin_kiosk_revoke(device_id):
    """Revocable from any admin session — doesn't require physical access to the kiosk."""
    device = KioskDevice.query.get_or_404(device_id)
    try:
        label = device.label or 'that device'
        db.session.delete(device)
        db.session.commit()
        flash(f'Revoked kiosk access for {label}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not revoke device: {e}', 'error')
    return redirect(url_for('admin_kiosk'))


# ─── Users & Permissions ──────────────────────────────────────────────────────

def _user_form_permissions():
    return {
        'is_admin':    bool(request.form.get('is_admin')),
        'can_people':  bool(request.form.get('can_people')),
        'can_devices': bool(request.form.get('can_devices')),
        'can_loaners': bool(request.form.get('can_loaners')),
        'can_repairs': bool(request.form.get('can_repairs')),
    }


@app.route('/admin/users')
@require_permission('admin')
def admin_users():
    users = User.query.order_by(User.username).all()
    return render_template('admin_users.html', users=users)


@app.route('/admin/users/new', methods=['GET', 'POST'])
@require_permission('admin')
def admin_user_new():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('admin_user_form.html', user=None)
        if User.query.filter(db.func.lower(User.username) == username.lower()).first():
            flash(f'Username "{username}" is already taken.', 'error')
            return render_template('admin_user_form.html', user=None)

        user = User(username=username, password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
                     **_user_form_permissions())
        db.session.add(user)
        db.session.commit()
        flash(f'Created user "{username}".', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin_user_form.html', user=None)


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@require_permission('admin')
def admin_user_edit(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        new_password = request.form.get('password', '')
        for field, value in _user_form_permissions().items():
            setattr(user, field, value)
        user.is_active = bool(request.form.get('is_active'))
        if new_password:
            user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        db.session.commit()
        flash(f'Updated user "{user.username}".', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin_user_form.html', user=user)


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@require_permission('admin')
def admin_user_delete(user_id):
    user = User.query.get_or_404(user_id)
    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'Deleted user "{username}".', 'success')
    return redirect(url_for('admin_users'))


# ─── Overdue Reminders ────────────────────────────────────────────────────────

def _overdue_assignments():
    """Open assignments (not yet returned) whose due_date has passed."""
    today = datetime.utcnow().date()
    return AssignmentHistory.query.filter(
        AssignmentHistory.unassigned_at.is_(None),
        AssignmentHistory.due_date.isnot(None),
        AssignmentHistory.due_date < today,
    ).order_by(AssignmentHistory.due_date).all()


@app.route('/admin/reminders')
@require_permission('admin')
def admin_reminders():
    overdue = _overdue_assignments()
    today = datetime.utcnow().date()
    return render_template('admin_reminders.html', overdue=overdue, today=today,
                           email_enabled=EMAIL_ENABLED)


@app.route('/admin/reminders/send', methods=['POST'])
@require_permission('admin')
def admin_reminders_send():
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_USERNAME and SMTP_PASSWORD in .env to enable it.', 'info')
        return redirect(url_for('admin_reminders'))

    overdue = _overdue_assignments()
    sent, failed, skipped = 0, 0, 0

    for row in overdue:
        person = Person.query.get(row.person_id) if row.person_id else None
        if not person:
            skipped += 1
            continue
        days_overdue = (datetime.utcnow().date() - row.due_date).days
        subject = f'Reminder: {row.asset_tag} is overdue for return'
        body = (
            f'Hi {person.first_name},\n\n'
            f'Our records show asset {row.asset_tag} was due back on '
            f'{row.due_date.strftime("%Y-%m-%d")} ({days_overdue} day{"s" if days_overdue != 1 else ""} ago).\n\n'
            'Please return it as soon as possible. If you\'ve already returned it, this reminder can be ignored.\n\n'
            'Thanks!'
        )
        try:
            send_email(person.email, subject, body)
            row.reminder_sent_at = datetime.utcnow()
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Reminder email failed for %s -> %s: %s', row.asset_tag, person.email, e)

    db.session.commit()

    msg = f'Sent {sent} reminder{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'error')
    return redirect(url_for('admin_reminders'))


# ─── Loaners ──────────────────────────────────────────────────────────────────

LOANER_REMINDER_RESEND_HOURS = 24
LOANER_DEFAULT_LOAN_DAYS = 7


@app.route('/admin/assets/<string:asset_tag>/toggle_loaner', methods=['POST'])
@require_permission('devices')
def admin_toggle_loaner(asset_tag):
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first_or_404()
    row.is_loaner = not row.is_loaner
    db.session.commit()
    flash(f'{asset_tag} is {"now" if row.is_loaner else "no longer"} in the loaner pool.', 'success')
    return redirect(request.referrer or url_for('admin_loaners'))


def _overdue_loaners():
    """Open loaner checkouts (not yet returned) whose due_date has passed."""
    today = datetime.utcnow().date()
    return LoanerCheckout.query.filter(
        LoanerCheckout.checked_in_at.is_(None),
        LoanerCheckout.due_date.isnot(None),
        LoanerCheckout.due_date < today,
    ).order_by(LoanerCheckout.due_date).all()


def _send_overdue_loaner_reminders():
    """
    Emails anyone with an overdue loaner. Safe to call repeatedly (e.g. from a
    background loop) — reminder_sent_at gates re-sending to once per
    LOANER_REMINDER_RESEND_HOURS, so it won't spam the same student hourly.
    Returns (sent, failed, skipped) counts.
    """
    if not EMAIL_ENABLED:
        return 0, 0, 0

    sent = failed = skipped = 0
    now = datetime.utcnow()
    for row in _overdue_loaners():
        if row.reminder_sent_at and (now - row.reminder_sent_at).total_seconds() < LOANER_REMINDER_RESEND_HOURS * 3600:
            continue
        person = Person.query.get(row.person_id) if row.person_id else None
        if not person:
            skipped += 1
            continue
        days_overdue = (now.date() - row.due_date).days
        subject = f'Reminder: loaner {row.asset_tag} is overdue for return'
        body = (
            f'Hi {person.first_name},\n\n'
            f'Our records show loaner device {row.asset_tag} was due back on '
            f'{row.due_date.strftime("%Y-%m-%d")} ({days_overdue} day{"s" if days_overdue != 1 else ""} ago).\n\n'
            'Please return it to the office as soon as possible. If you\'ve already returned it, '
            'this reminder can be ignored.\n\nThanks!'
        )
        try:
            send_email(person.email, subject, body)
            row.reminder_sent_at = now
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Loaner reminder email failed for %s -> %s: %s', row.asset_tag, person.email, e)

    db.session.commit()
    return sent, failed, skipped


def _loaner_reminder_loop():
    """Background daemon: checks for overdue loaners once an hour so students
    get emailed automatically without anyone having to click a button. The
    reminder_sent_at gate in _send_overdue_loaner_reminders() keeps this safe
    even though gunicorn runs multiple worker processes, each with their own
    copy of this loop."""
    while True:
        time.sleep(3600)
        try:
            with app.app_context():
                _send_overdue_loaner_reminders()
        except Exception as e:
            logger.error('Loaner reminder background loop error: %s', e)


if EMAIL_ENABLED:
    threading.Thread(target=_loaner_reminder_loop, daemon=True).start()


@app.route('/admin/loaners')
@require_permission('loaners')
def admin_loaners():
    loaner_rows = AssetRegistry.query.filter_by(is_loaner=True).order_by(AssetRegistry.asset_tag).all()
    tags = [r.asset_tag for r in loaner_rows]
    open_checkouts = {
        c.asset_tag: c for c in LoanerCheckout.query.filter(
            LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.is_(None)
        )
    }
    overdue_count = len(_overdue_loaners())
    return render_template('admin_loaners.html', loaner_rows=loaner_rows, open_checkouts=open_checkouts,
                           overdue_count=overdue_count, email_enabled=EMAIL_ENABLED,
                           today=datetime.utcnow().date())


@app.route('/admin/loaners/send_reminders', methods=['POST'])
@require_permission('loaners')
def admin_loaners_send_reminders():
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_USERNAME and SMTP_PASSWORD in .env to enable it.', 'info')
        return redirect(url_for('admin_loaners'))
    sent, failed, skipped = _send_overdue_loaner_reminders()
    msg = f'Sent {sent} reminder{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'info')
    return redirect(url_for('admin_loaners'))


def _checkout_loaner(asset_tag, person, due_date=None):
    """Shared checkout logic used by both the admin page and student self-service."""
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag, is_loaner=True).first()
    if not row:
        return 'error', f'{asset_tag} is not a loaner device.'
    already_out = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if already_out:
        return 'error', f'{asset_tag} is already checked out to {already_out.person_name}.'
    db.session.add(LoanerCheckout(
        asset_tag=asset_tag, person_id=person.id, person_name=person.full_name,
        due_date=due_date or (datetime.utcnow().date() + timedelta(days=LOANER_DEFAULT_LOAN_DAYS)),
    ))
    db.session.commit()
    return 'ok', f'Checked out {asset_tag} to {person.full_name}.'


def _checkin_loaner(asset_tag, condition_notes=None):
    """Shared checkin logic used by both the admin page and student self-service."""
    open_row = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if not open_row:
        return 'error', f'{asset_tag} is not currently checked out as a loaner.'
    open_row.checked_in_at = datetime.utcnow()
    if condition_notes:
        open_row.condition_notes = condition_notes
    db.session.commit()
    return 'ok', f'Checked in {asset_tag} (was with {open_row.person_name}).'


@app.route('/admin/loaners/checkout', methods=['POST'])
@require_permission('loaners')
def admin_loaners_checkout():
    asset_tag = request.form.get('asset_tag', '').strip()
    person_id = request.form.get('person_id', '').strip()
    due_date_str = request.form.get('due_date', '').strip()
    person = Person.query.get(int(person_id)) if person_id.isdigit() else None
    if not asset_tag or not person:
        flash('Choose both a person and a loaner asset tag.', 'error')
        return redirect(url_for('admin_loaners'))
    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid due date.', 'error')
            return redirect(url_for('admin_loaners'))
    status, message = _checkout_loaner(asset_tag, person, due_date)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(url_for('admin_loaners'))


@app.route('/admin/loaners/checkin', methods=['POST'])
@require_permission('loaners')
def admin_loaners_checkin():
    asset_tag = request.form.get('asset_tag', '').strip()
    status, message = _checkin_loaner(asset_tag)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(url_for('admin_loaners'))


@app.route('/loaner_checkout', methods=['GET', 'POST'])
@kiosk_or_login_required
def loaner_checkout_page():
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        scan_value = request.form.get('scan_value', '').strip()
        person = Person.query.get(int(person_id)) if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            flash(f'"{scan_value}" was not found in the asset registry.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        status, message = _checkout_loaner(asset_tag, person)
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkout_page'))

    return render_template('loaner_checkout.html')


@app.route('/loaner_checkin', methods=['GET', 'POST'])
@kiosk_or_login_required
def loaner_checkin_page():
    if request.method == 'POST':
        scan_value = request.form.get('scan_value', '').strip()
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkin_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value so a direct tag match on LoanerCheckout still works
        status, message = _checkin_loaner(asset_tag)
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkin_page'))

    return render_template('loaner_checkin.html')


# ─── Asset Audit ──────────────────────────────────────────────────────────────

@app.route('/admin/audit')
@require_permission('devices')
def admin_audit():
    """
    Physical inventory check: scan every device you can find, and anything
    in scope that hasn't been scanned since the audit start date shows up as
    "missing" — the actionable list of devices to go track down.
    """
    since_str = request.args.get('since', '').strip()
    try:
        since = datetime.strptime(since_str, '%Y-%m-%d') if since_str else None
    except ValueError:
        since = None
    if not since:
        since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    since_str = since.strftime('%Y-%m-%d')

    status_filter = request.args.get('status', '').strip()
    type_filter = request.args.get('device_type', '').strip()

    query = AssetRegistry.query.order_by(AssetRegistry.asset_tag)
    if status_filter in ASSET_STATUSES:
        query = _filter_registry_by_status(query, status_filter)
    else:
        status_filter = ''
    if type_filter in DEVICE_TYPES:
        query = query.filter(AssetRegistry.device_type == type_filter)
    else:
        type_filter = ''

    in_scope = query.all()
    scanned_tags = {t for (t,) in db.session.query(AuditScan.asset_tag)
                    .filter(AuditScan.scanned_at >= since).distinct()}
    missing = [r for r in in_scope if r.asset_tag not in scanned_tags]

    return render_template('admin_audit.html', since=since_str, status_filter=status_filter,
                           type_filter=type_filter, asset_statuses=ASSET_STATUSES, device_types=DEVICE_TYPES,
                           verified_count=len(in_scope) - len(missing), total_count=len(in_scope), missing=missing)


@app.route('/admin/audit/scan', methods=['POST'])
@require_permission('devices')
def admin_audit_scan():
    value = request.form.get('value', '').strip()
    redirect_args = {k: request.form.get(k, '') for k in ('since', 'status', 'device_type') if request.form.get(k)}

    if not value:
        return redirect(url_for('admin_audit', **redirect_args))

    asset_tag, _ = resolve_scan(value)
    if not asset_tag:
        flash(f'No asset found matching "{value}".', 'error')
        return redirect(url_for('admin_audit', **redirect_args))

    db.session.add(AuditScan(asset_tag=asset_tag))
    db.session.commit()
    flash(f'Verified {asset_tag}.', 'success')
    return redirect(url_for('admin_audit', **redirect_args))


# ─── Incident / Damage Reports ────────────────────────────────────────────────

@app.route('/admin/assets/<string:asset_tag>/incidents', methods=['POST'])
@require_permission('devices')
def admin_incident_add(asset_tag):
    """Logs a damage/loss report against an asset, snapshotting the currently assigned person."""
    AssetRegistry.query.filter_by(asset_tag=asset_tag).first_or_404()
    description = request.form.get('description', '').strip()
    fee_charged = request.form.get('fee_charged') == 'on'
    if not description:
        flash('Enter a description of the incident.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None
    try:
        db.session.add(Incident(
            asset_tag=asset_tag, person_id=person.id if person else None,
            person_name=person.full_name if person else None,
            description=description, fee_charged=fee_charged,
        ))
        db.session.commit()
        flash('Incident logged.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not log incident: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


# ─── Scan / Check-in / Check-out API ─────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
@kiosk_or_api_login_required
def scan_asset():
    """
    Unified scan endpoint.
    Body: { "scan_value": "<asset tag or serial number>", "action": "checkin"|"checkout" }

    If the scan value is in the registry → normal operation.
    If not → record it as an orphan (is_valid=False) so it heals on next CSV upload.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    scan_value = (data.get('scan_value') or '').strip()
    action     = (data.get('action') or '').strip().lower()

    if not scan_value:
        return jsonify({'error': 'scan_value is required'}), 400
    if action not in ('checkin', 'checkout'):
        return jsonify({'error': 'action must be checkin or checkout'}), 400

    # Sanity-check length rather than requiring an exact match — different device
    # brands use different serial/service-tag lengths (e.g. 10-char HP serials vs.
    # 7-char Dell Service Tags), so this only catches obviously-wrong scans
    # (empty, or way too short/long to be any real tag or serial).
    if not (4 <= len(scan_value) <= 20):
        return jsonify({
            'error': f'Invalid scan: "{scan_value}" is {len(scan_value)} characters — that doesn\'t look like a valid asset tag or serial number.',
            'invalid_format': True,
        }), 400

    try:
        asset_tag, scan_type = resolve_scan(scan_value)
        unknown = asset_tag is None

        if unknown:
            # Store using the raw scan value as the asset_tag placeholder
            asset_tag = scan_value
            scan_type = 'unknown'

        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=not unknown)
            db.session.add(asset)
        elif unknown and not asset.is_valid:
            pass  # stays invalid until CSV heals it
        elif not unknown:
            asset.is_valid = True

        if action == 'checkin':
            asset.check_in  = datetime.utcnow()
            asset.check_out = None
        else:
            if not asset.check_in:
                return jsonify({'error': f'Asset {asset_tag} has not been checked in yet'}), 409
            asset.check_out = datetime.utcnow()

        event = Event(
            asset_tag=asset_tag,
            action=action,
            scanned_value=scan_value,
            scan_type=scan_type,
            person_name=asset.assigned_to.full_name if asset.assigned_to else None,
        )
        db.session.add(event)
        db.session.commit()

        return jsonify({
            'message':   f'Asset {action} successful',
            'asset_tag': asset_tag,
            'scan_type': scan_type,
            'is_valid':  asset.is_valid,
            'warning':   'Asset not found in registry – will heal on next CSV import' if unknown else None,
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error('Scan error: %s', e)
        return jsonify({'error': 'Internal Server Error'}), 500


# ─── Public API ───────────────────────────────────────────────────────────────

@app.route('/api/assets', methods=['GET'])
@api_login_required
def get_assets():
    try:
        assets = Asset.query.order_by(Asset.asset_tag).all()
        return jsonify([a.to_dict() for a in assets])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/assets/<string:asset_tag>/history', methods=['GET'])
@api_login_required
def get_asset_history(asset_tag):
    events = Event.query.filter_by(asset_tag=asset_tag).order_by(Event.timestamp.desc()).all()
    if not events:
        return jsonify({'message': f'No history found for {asset_tag}'}), 404
    return jsonify([e.to_dict() for e in events])


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.route('/')
@kiosk_or_login_required
def index():
    return render_template('index.html')


@app.route('/checkin')
@kiosk_or_login_required
def checkin_page():
    recent = Event.query.filter_by(action='checkin').order_by(Event.timestamp.desc()).limit(10).all()
    return render_template('scan.html', action='checkin', title='Check In', recent_events=recent)


@app.route('/checkout')
@kiosk_or_login_required
def checkout_page():
    recent = Event.query.filter_by(action='checkout').order_by(Event.timestamp.desc()).limit(10).all()
    return render_template('scan.html', action='checkout', title='Check Out', recent_events=recent)


@app.route('/asset_history')
@login_required
def asset_history():
    query = request.args.get('q', '').strip()
    if not query:
        return render_template('asset_history.html', query=None, resolved_tag=None, events=[])

    # Try to resolve via registry (asset tag or serial number)
    asset_tag, _ = resolve_scan(query)

    # If not in registry, fall back to searching events directly
    if not asset_tag:
        asset_tag = query

    events = Event.query.filter_by(asset_tag=asset_tag).order_by(Event.timestamp.desc()).all()
    return render_template('asset_history.html',
                           query=query,
                           resolved_tag=asset_tag if asset_tag != query else None,
                           events=events)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=DEBUG_MODE)