from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, session, flash, has_request_context
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone, timedelta
from functools import wraps
import os
import re
import csv
import io
import time
import threading
import secrets
import smtplib
import logging
import sys
import colorsys
from email.message import EmailMessage
from decimal import Decimal, InvalidOperation
from collections import defaultdict, OrderedDict
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

# ─── Google Workspace sync config ──────────────────────────────────────────────
# One service account handles both the read-only info sync and (if opted into
# separately below) the loaner auto-disable write path — its Domain-wide
# Delegation entry in the Workspace Admin console just needs both scopes
# authorized on the same Client ID, not two separate service accounts.
GOOGLE_SERVICE_ACCOUNT_FILE    = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE')
GOOGLE_ADMIN_IMPERSONATE_EMAIL = os.environ.get('GOOGLE_ADMIN_IMPERSONATE_EMAIL')
GOOGLE_SYNC_ENABLED = bool(GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_ADMIN_IMPERSONATE_EMAIL)
GOOGLE_SCOPE_READONLY = 'https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly'
GOOGLE_SCOPE_MANAGE   = 'https://www.googleapis.com/auth/admin.directory.device.chromeos'

# Remotely disabling a live device is a much bigger blast radius than the
# read-only sync above, so it gets its own separate opt-in on top of
# GOOGLE_SYNC_ENABLED — and a per-site flag on top of that (see Site.google_loaner_autodisable_enabled).
GOOGLE_LOANER_AUTO_DISABLE_ENABLED = os.environ.get('GOOGLE_LOANER_AUTO_DISABLE_ENABLED', '').lower() in ('1', 'true', 'yes')

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
BRANDING_ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.svg', '.webp'}

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


# ─── Models ───────────────────────────────────────────────────────────────────

class Site(db.Model):
    """A school/building. The unit that people, devices, and admin users are scoped to."""
    __tablename__ = 'site'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), unique=True, nullable=False, index=True)
    google_loaner_autodisable_enabled = db.Column(db.Boolean, nullable=False, default=False)  # per-site opt-in pilot gate, see _sync_loaner_google_state
    logo_filename = db.Column(db.String(255), nullable=True)  # overrides BrandingSettings.logo_filename in the nav for this site's users; falls back when unset
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class BrandingSettings(db.Model):
    """
    Single-row (id=1) app-wide branding config, editable at /admin/branding.
    primary_color_raw is exactly what the admin picked in the color input —
    kept separate from the derived columns so re-opening the settings form
    shows their actual choice, not a contrast-nudged value that would drift
    further every time they saved. Every other *_color column is computed by
    generate_palette() at save time (see the color engine above the routes)
    and cached here so normal page loads never re-run the color math.
    """
    __tablename__ = 'branding_settings'
    id                   = db.Column(db.Integer, primary_key=True)
    app_name             = db.Column(db.String(120), nullable=True)
    logo_filename        = db.Column(db.String(255), nullable=True)
    primary_color_raw    = db.Column(db.String(7), nullable=True)
    primary_color        = db.Column(db.String(7), nullable=True)  # = --accent (contrast-nudged for readability on the dark bg)
    accent_dim_color     = db.Column(db.String(7), nullable=True)
    accent_text_color    = db.Column(db.String(7), nullable=True)
    secondary_color      = db.Column(db.String(7), nullable=True)
    secondary_text_color = db.Column(db.String(7), nullable=True)
    tertiary_color       = db.Column(db.String(7), nullable=True)
    tertiary_text_color  = db.Column(db.String(7), nullable=True)
    updated_at           = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserSite(db.Model):
    """Join table: which sites a (non-super-admin) User account can see/manage."""
    __tablename__ = 'user_site'
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint('user_id', 'site_id', name='uq_user_site'),)


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
    site_id       = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True, index=True)
    purchase_date = db.Column(db.Date, nullable=True)
    purchase_cost = db.Column(db.Numeric(10, 2), nullable=True)
    warranty_expiration = db.Column(db.Date, nullable=True)

    site = db.relationship('Site')

    def to_dict(self):
        return {
            'asset_tag': self.asset_tag,
            'serial_number': self.serial_number,
            'description': self.description,
            'device_type': self.device_type,
            'is_loaner': self.is_loaner,
            'site': self.site.name if self.site else None,
            'purchase_date': self.purchase_date.isoformat() if self.purchase_date else None,
            'purchase_cost': str(self.purchase_cost) if self.purchase_cost is not None else None,
            'warranty_expiration': self.warranty_expiration.isoformat() if self.warranty_expiration else None,
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
    google_enabled     = db.Column(db.Boolean, nullable=True)  # last known enabled/disabled state, set by the loaner auto-disable sync

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
    site_legacy = db.Column('site', db.String(120), nullable=True, index=True)  # old free-text site column, kept for the one-time backfill only — use `site` (the relationship) everywhere else
    site_id    = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True, index=True)
    external_id = db.Column(db.String(40), unique=True, nullable=True, index=True)  # district staff/student ID — bulk-import upsert key
    grad_year  = db.Column(db.Integer, nullable=True, index=True)  # expected graduation year (students); blank for staff
    is_active  = db.Column(db.Boolean, nullable=False, default=True, index=True)  # False once graduated/withdrawn — keeps history/incidents intact instead of deleting
    insurance_opted_in = db.Column(db.Boolean, nullable=False, default=False)  # family paid for the device protection plan this year
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    site = db.relationship('Site')

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
            'site':        self.site.name if self.site else None,
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
    acknowledged_by = db.Column(db.String(160), nullable=True)  # typed name acknowledging responsibility at assign time


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
    is_admin      = db.Column(db.Boolean, nullable=False, default=False)  # full permission *within whatever sites this user has*, incl. managing users
    is_super_admin = db.Column(db.Boolean, nullable=False, default=False)  # sees/manages every site, independent of is_admin
    can_people    = db.Column(db.Boolean, nullable=False, default=False)
    can_devices   = db.Column(db.Boolean, nullable=False, default=False)
    can_devices_manage = db.Column(db.Boolean, nullable=False, default=False)  # add/edit/remove devices, set sites, mark loaners — implies can_devices too
    can_loaners   = db.Column(db.Boolean, nullable=False, default=False)
    can_loaner_checkinout = db.Column(db.Boolean, nullable=False, default=False)  # just processing loaner checkout/checkin, not the full pool — implied by can_loaners too
    can_checkinout = db.Column(db.Boolean, nullable=False, default=False)  # the plain device Check In / Check Out pages + /api/scan
    can_repairs   = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_users = db.Column(db.Boolean, nullable=False, default=False)  # add/edit/delete User accounts, narrower than is_admin (can't grant is_admin/is_super_admin)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    sites = db.relationship('Site', secondary='user_site', backref='users')


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
    site_id    = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    site = db.relationship('Site')


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
    fee_amount  = db.Column(db.Numeric(8, 2), nullable=True)
    paid_at     = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Repair(db.Model):
    """
    A device sent out for RMA/repair — separate from the plain 'repair' Asset
    status label, this is the actual tracking record (vendor, ticket, dates).
    Wired to the can('repairs') permission. person_name_snapshot mirrors the
    same pattern as Incident/AssignmentHistory: readable even if the person
    who had the device is later deleted.
    """
    __tablename__ = 'repair'
    id                   = db.Column(db.Integer, primary_key=True)
    asset_tag            = db.Column(db.String(120), nullable=False, index=True)
    vendor               = db.Column(db.String(160), nullable=True)
    ticket_number        = db.Column(db.String(80), nullable=True)
    issue_description    = db.Column(db.Text, nullable=True)
    person_name_snapshot = db.Column(db.String(160), nullable=True)
    sent_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expected_return_at   = db.Column(db.Date, nullable=True)
    returned_at          = db.Column(db.DateTime, nullable=True)
    outcome              = db.Column(db.String(20), nullable=True)  # 'fixed' | 'could_not_repair' | 'replaced'
    notes                = db.Column(db.Text, nullable=True)


REPAIR_OUTCOMES = {
    'fixed': 'Fixed',
    'could_not_repair': 'Could Not Repair',
    'replaced': 'Replaced',
}


class ActivityLog(db.Model):
    """
    An accountability trail of admin-side mutations — who did what, when.
    Deliberately does NOT log every /api/scan check-in/out (that volume is
    already fully captured by Event); this is for the things nothing else
    records an actor for: people/devices/loaners/users/sites/repairs/fees.
    site_id is best-effort (None for things like Users/Sites CRUD or a bulk
    import spanning multiple sites) — a site-scoped admin only sees rows
    with a matching site_id, so leaving it None makes a row super-admin-only.
    """
    __tablename__ = 'activity_log'
    id            = db.Column(db.Integer, primary_key=True)
    timestamp     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    actor_type    = db.Column(db.String(20), nullable=False)  # 'user' | 'legacy_admin' | 'kiosk' | 'system'
    actor_label   = db.Column(db.String(160), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    site_id       = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True, index=True)
    action        = db.Column(db.String(60), nullable=False, index=True)
    summary       = db.Column(db.Text, nullable=False)


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
    acknowledged_by  = db.Column(db.String(160), nullable=True)  # typed name acknowledging responsibility at checkout


# Schema creation/upgrades are handled by Flask-Migrate (`flask db upgrade`),
# run once from entrypoint.sh before gunicorn starts — not here. Running it
# in-process per gunicorn worker (the old db.create_all() approach) doesn't
# work safely with Alembic's single version-tracking table the way it did
# with create_all()'s idempotent CREATE TABLE IF NOT EXISTS-like behavior.
# For local dev outside Docker, run `flask db upgrade` once yourself first.


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


def _save_branding_logo(file_storage, prefix):
    """
    Validates and saves an uploaded logo, returning its on-disk filename (to
    store on BrandingSettings.logo_filename or Site.logo_filename) or None if
    no file was submitted. Raises ValueError on an invalid extension.

    A random suffix busts browser/CDN caching when a logo is replaced — the
    old URL (old filename) simply stops resolving rather than serving a
    stale cached image at a now-reused path.
    """
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in BRANDING_ALLOWED_EXTENSIONS:
        raise ValueError(f'Unsupported file type "{ext}". Use PNG, JPG, SVG, or WebP.')
    filename = f'{prefix}_{secrets.token_hex(4)}{ext}'
    file_storage.save(os.path.join(BRANDING_UPLOAD_DIR, filename))
    return filename


def _delete_branding_logo(filename):
    if not filename:
        return
    path = os.path.join(BRANDING_UPLOAD_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ─── Branding color engine ──────────────────────────────────────────────────
# Pure color math — no DB access. generate_palette() is the entry point,
# called once from the /admin/branding save route (not on every page load).

APP_BG_HEX = '#0f1117'  # must track :root's --bg in base.html — see generate_palette()
_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f'#{r:02x}{g:02x}{b:02x}'


def _relative_luminance(rgb):
    """WCAG 2.x relative luminance (0=black, 1=white)."""
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_ratio(rgb_a, rgb_b):
    """WCAG contrast ratio, 1 (no contrast) to 21 (black vs white)."""
    l1 = _relative_luminance(rgb_a) + 0.05
    l2 = _relative_luminance(rgb_b) + 0.05
    return max(l1, l2) / min(l1, l2)


def _best_text_color(bg_hex):
    """Whichever of black/white reads better on top of bg_hex."""
    bg_rgb = _hex_to_rgb(bg_hex)
    return '#000000' if _contrast_ratio((0, 0, 0), bg_rgb) >= _contrast_ratio((255, 255, 255), bg_rgb) else '#ffffff'


def _ensure_min_contrast(hex_color, against_hex, min_ratio=4.5):
    """
    Nudges hex_color's HSL lightness toward whichever direction increases
    contrast against against_hex, stepping until min_ratio is met or the
    lightness bound (0/1) is hit. Preserves hue/saturation — a nudged brand
    red stays recognizably that red, just legible against a near-black page.
    """
    rgb = _hex_to_rgb(hex_color)
    against_rgb = _hex_to_rgb(against_hex)
    if _contrast_ratio(rgb, against_rgb) >= min_ratio:
        return hex_color

    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in rgb))
    direction = 1 if _relative_luminance(against_rgb) < 0.5 else -1
    result_hex = hex_color
    for _ in range(40):
        l = min(1.0, max(0.0, l + direction * 0.02))
        candidate_rgb = tuple(c * 255.0 for c in colorsys.hls_to_rgb(h, l, s))
        result_hex = _rgb_to_hex(candidate_rgb)
        if _contrast_ratio(candidate_rgb, against_rgb) >= min_ratio or l <= 0.0 or l >= 1.0:
            break
    return result_hex


def _rotated_hue(hex_color, degrees):
    rgb = _hex_to_rgb(hex_color)
    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in rgb))
    h = (h + degrees / 360.0) % 1.0
    return _rgb_to_hex(tuple(c * 255.0 for c in colorsys.hls_to_rgb(h, l, min(1.0, s * 0.92))))


def generate_palette(primary_hex, bg_hex=APP_BG_HEX):
    """
    Given one admin-picked brand color, derives a full contrast-safe palette
    for this app's dark theme:
      - accent: primary_hex, nudged (if needed) to >=4.5:1 against bg_hex —
        used both as a solid fill (buttons/badges) and as standalone text/
        links directly on the page background, matching how --accent is
        already used throughout the existing CSS.
      - accent_dim: a darker/desaturated variant for hover states.
      - accent_text: best(black, white) for text drawn on top of accent.
      - secondary/tertiary: +140/-140 degree hue rotations of accent (a
        split-complementary-ish spread — distinct from primary without the
        harsher clash of a true 180 degree complement), each independently
        nudged for >=4.5:1 against bg_hex, with their own best-contrast text
        color.
    Returns a dict of hex strings, all direct CSS custom-property values.
    """
    accent = _ensure_min_contrast(primary_hex, bg_hex, min_ratio=4.5)
    accent_rgb = _hex_to_rgb(accent)
    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in accent_rgb))

    dim_l = max(0.0, l * 0.55)
    accent_dim = _rgb_to_hex(tuple(c * 255.0 for c in colorsys.hls_to_rgb(h, dim_l, s)))

    secondary = _ensure_min_contrast(_rotated_hue(accent, 140), bg_hex, min_ratio=4.5)
    tertiary = _ensure_min_contrast(_rotated_hue(accent, -140), bg_hex, min_ratio=4.5)

    return {
        'accent': accent,
        'accent_dim': accent_dim,
        'accent_text': _best_text_color(accent),
        'secondary': secondary,
        'secondary_text': _best_text_color(secondary),
        'tertiary': tertiary,
        'tertiary_text': _best_text_color(tertiary),
    }


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


def _google_directory_service(scopes):
    """
    Builds an authenticated Admin SDK Directory API client using domain-wide
    delegation — the service account impersonates GOOGLE_ADMIN_IMPERSONATE_EMAIL
    (a real Workspace super admin) so its calls act with that admin's authority.
    Raises FileNotFoundError/ValueError from the google-auth library itself if
    GOOGLE_SERVICE_ACCOUNT_FILE doesn't point at a valid key file — callers
    don't need to catch that separately, the routes already flash str(e).
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes,
    ).with_subject(GOOGLE_ADMIN_IMPERSONATE_EMAIL)
    return build('admin', 'directory_v1', credentials=credentials, cache_discovery=False)


def _find_chromeos_device_by_serial(service, serial_number):
    """
    Paginates the domain's Chrome device list looking for a serial number
    match. Filters client-side rather than using the API's `query` parameter —
    serial-number search via `query` isn't reliably documented/stable, while
    paging through `list()` (100 devices/page) is guaranteed-correct at
    typical K-12 fleet sizes. Returns the device dict, or None if not found.
    """
    page_token = None
    while True:
        response = service.chromeosdevices().list(
            customerId='my_customer', maxResults=100, pageToken=page_token, projection='FULL',
        ).execute()
        for device in response.get('chromeosdevices', []):
            if device.get('serialNumber') == serial_number:
                return device
        page_token = response.get('nextPageToken')
        if not page_token:
            return None


def sync_chromeos_device_from_google(serial_number):
    """
    Looks up a Chromebook by serial number via the Google Admin SDK Directory API
    and returns its model, org unit, and most recently synced user. Requires a
    Google Cloud service account with domain-wide delegation authorized (in the
    Workspace Admin console) for the
    https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly
    scope, impersonating a super admin (GOOGLE_ADMIN_IMPERSONATE_EMAIL). See
    /admin/google_setup for a step-by-step walkthrough of that setup.

    Args:
        serial_number: The device's manufacturer serial number.

    Returns:
        A dict with keys 'model', 'org_unit', 'recent_user'.

    Raises:
        LookupError: No Chrome device with this serial number exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_READONLY])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    recent_users = device.get('recentUsers') or []
    return {
        'model': device.get('model'),
        'org_unit': device.get('orgUnitPath'),
        'recent_user': recent_users[0].get('email') if recent_users else None,
    }


def set_chromeos_device_enabled(serial_number, enabled):
    """
    Enables or disables a Chromebook in Google Workspace by serial number —
    resolves the device ID, then calls the Admin SDK's chromeosdevices().action()
    with action='reenable' (enabled=True) or 'disable' (enabled=False).

    Needs the SAME service account as sync_chromeos_device_from_google() above,
    but with the additional (non-readonly) write scope
    https://www.googleapis.com/auth/admin.directory.device.chromeos authorized
    on its Domain-wide Delegation entry too — not a separate service account,
    just an extra scope on the same Client ID.

    Args:
        serial_number: The device's manufacturer serial number.
        enabled: True to re-enable, False to disable.

    Raises:
        LookupError: No Chrome device with this serial number exists in the domain.
    """
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    device = _find_chromeos_device_by_serial(service, serial_number)
    if not device:
        raise LookupError(f'No Chromebook with serial number "{serial_number}" found in Google Workspace.')
    service.chromeosdevices().action(
        customerId='my_customer', resourceId=device['deviceId'],
        body={'action': 'reenable' if enabled else 'disable'},
    ).execute()


def _sync_loaner_google_state(registry_row, enabled):
    """
    Best-effort Google enable/disable for a loaner Chromebook on checkout/checkin.
    No-ops (logs and returns) unless GOOGLE_SYNC_ENABLED, the separate
    GOOGLE_LOANER_AUTO_DISABLE_ENABLED env var, AND this device's site's opt-in
    flag are all true, and the device has a serial number on file. Never raises
    and never touches db.session — the checkout/checkin has already committed
    by the time this runs, so a Google-side failure shouldn't roll back the
    local checkout/checkin or block the person waiting on it.
    """
    if not (GOOGLE_SYNC_ENABLED and GOOGLE_LOANER_AUTO_DISABLE_ENABLED):
        return
    if not (registry_row.site and registry_row.site.google_loaner_autodisable_enabled):
        return
    if not registry_row.serial_number:
        logger.info('Skipping Google loaner auto-%s for %s: no serial number on file.',
                    'enable' if enabled else 'disable', registry_row.asset_tag)
        return
    try:
        set_chromeos_device_enabled(registry_row.serial_number, enabled)
        asset = Asset.query.filter_by(asset_tag=registry_row.asset_tag).first()
        if asset:
            asset.google_enabled = enabled
            db.session.commit()
    except Exception as e:
        logger.error('Google loaner auto-%s failed for %s: %s',
                     'enable' if enabled else 'disable', registry_row.asset_tag, e)


def send_email(to_email, subject, body):
    """
    Sends a plain-text email via SMTP (Gmail by default: smtp.gmail.com:587 with
    an App Password — a regular account password will not work with 2FA enabled).
    Also works against an IP-allowlisted Google Workspace SMTP relay
    (smtp-relay.gmail.com) with no SMTP_USERNAME/SMTP_PASSWORD set at all —
    login() is only attempted when both are present.

    Args:
        to_email: Recipient address.
        subject: Email subject line.
        body: Plain-text email body.

    Raises:
        RuntimeError: If SMTP_FROM_EMAIL is not configured.
        smtplib.SMTPException, OSError: On connection/authentication/send failure.
    """
    if not EMAIL_ENABLED:
        raise RuntimeError('Email is not configured (set SMTP_FROM_EMAIL in .env).')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM_EMAIL
    msg['To'] = to_email
    msg.set_content(body)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
        server.starttls()
        if SMTP_USERNAME and SMTP_PASSWORD:
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


def _current_site_ids():
    """
    None = unrestricted (super admin, or the legacy shared-password login).
    Otherwise the list of site_ids the current session may see/act on.
    Empty list = sees nothing — e.g. a named user not yet assigned any site,
    or a kiosk enrolled without one. Only meaningful inside a route already
    gated by login_required/require_permission/kiosk_or_login_required, since
    it trusts the session is already valid rather than re-checking expiry.
    """
    if session.get('admin_logged_in'):
        if session.get('is_super_admin'):
            return None
        user = _current_user()
        return [s.id for s in user.sites] if user else []
    token = request.cookies.get('kiosk_token')
    if token:
        device = KioskDevice.query.filter_by(token=token).first()
        return [device.site_id] if device and device.site_id else []
    return []


def _current_actor():
    """
    Resolves who's making the current request, for the activity log. Covers
    the three real 'logged in' states this app has (named User, legacy
    shared-password login, kiosk device) plus a 'system' fallback for code
    that runs with no request context (the hourly reminder background thread).
    Returns (actor_type, actor_label, actor_user_id).
    """
    if not has_request_context():
        return 'system', 'System (background job)', None
    if session.get('admin_logged_in'):
        user = _current_user()
        if user:
            return 'user', user.username, user.id
        return 'legacy_admin', 'Admin (shared login)', None
    token = request.cookies.get('kiosk_token')
    if token:
        device = KioskDevice.query.filter_by(token=token).first()
        if device:
            return 'kiosk', f'Kiosk: {device.label or device.token[:8]}', None
    return 'system', 'System (background job)', None


def _log_activity(action, summary, site_id=None):
    """
    Records an admin-side mutation. Never commits itself — call this before
    the route's own db.session.commit() so the log entry and the action it
    describes are always atomic. site_id is best-effort; leave it None for
    anything without one clear site (Users/Sites CRUD, a multi-site bulk import).
    """
    actor_type, actor_label, actor_user_id = _current_actor()
    db.session.add(ActivityLog(
        actor_type=actor_type, actor_label=actor_label, actor_user_id=actor_user_id,
        site_id=site_id, action=action, summary=summary,
    ))


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
        'devices': user.can_devices or user.can_devices_manage,
        'devices_manage': user.can_devices_manage,
        'loaners': user.can_loaners,
        'loaner_checkinout': user.can_loaner_checkinout or user.can_loaners,
        'checkinout': user.can_checkinout,
        'repairs': user.can_repairs,
        'manage_users': user.can_manage_users,
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


def require_super_admin(f):
    """Like require_permission, but for district-wide features (managing Sites,
    the full-registry CSV replace) that even a site-scoped is_admin=True user
    shouldn't be able to touch."""
    @wraps(f)
    def decorated(*args, **kwargs):
        was_logged_in = session.get('admin_logged_in')
        if not _admin_session_active():
            msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                   else 'Please log in to access the admin panel.')
            flash(msg, 'error')
            return redirect(url_for('admin_login'))
        if not session.get('is_super_admin'):
            flash('Only a super admin can access that page.', 'error')
            return redirect(url_for('admin_panel'))
        return f(*args, **kwargs)
    return decorated


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


def kiosk_or_permission_required(perm):
    """Like kiosk_or_login_required, but a logged-in (non-kiosk) session also
    needs the given area permission — a kiosk device's cookie always passes,
    since kiosks are physically dedicated to this one job."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if _kiosk_device_valid():
                return f(*args, **kwargs)
            was_logged_in = session.get('admin_logged_in')
            if not _admin_session_active():
                msg = ('Your session expired after 15 minutes of inactivity.' if was_logged_in
                       else 'Please log in, or use a device enrolled in Kiosk Mode.')
                flash(msg, 'error')
                return redirect(url_for('admin_login'))
            if not _has_permission(perm):
                flash('Your account doesn\'t have permission to access that page.', 'error')
                return redirect(url_for('admin_panel'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def kiosk_or_api_permission_required(perm):
    """Like kiosk_or_permission_required, but returns JSON errors instead of redirecting."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if _kiosk_device_valid():
                return f(*args, **kwargs)
            if not _admin_session_active():
                return jsonify({'error': 'Unauthorized. Log in or use a device enrolled in Kiosk Mode.'}), 401
            if not _has_permission(perm):
                return jsonify({'error': 'Your account doesn\'t have permission to do that.'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ─── Auth ─────────────────────────────────────────────────────────────────────

# Every value here is a literal match for what base.html's :root already
# hardcoded before branding existed — an unconfigured install (or a
# BrandingSettings field left blank) must look pixel-identical to today.
_DEFAULT_BRANDING = {
    'logo_url': None,
    'app_name': None,
    'accent': '#4f7ef8', 'accent_dim': '#2a4aaa', 'accent_text': '#ffffff',
    'secondary': '#f15e56', 'secondary_text': '#000000',
    'tertiary': '#b5f156', 'tertiary_text': '#000000',
}


def _current_branding():
    """
    Resolves the logo/colors for the current request: a site-specific logo
    (from a single-site admin login or a kiosk device tied to a site) takes
    priority over the district-wide default logo; colors are always the
    single global palette (see admin_branding()/generate_palette()) since
    real schools sharing a district brand generally share one color scheme
    even when their logos differ — split further into per-site colors later
    if that stops being true.
    """
    settings = BrandingSettings.query.get(1)
    branding = dict(_DEFAULT_BRANDING)
    if settings:
        branding['app_name']       = settings.app_name or None
        branding['accent']         = settings.primary_color or branding['accent']
        branding['accent_dim']     = settings.accent_dim_color or branding['accent_dim']
        branding['accent_text']    = settings.accent_text_color or branding['accent_text']
        branding['secondary']      = settings.secondary_color or branding['secondary']
        branding['secondary_text'] = settings.secondary_text_color or branding['secondary_text']
        branding['tertiary']       = settings.tertiary_color or branding['tertiary']
        branding['tertiary_text']  = settings.tertiary_text_color or branding['tertiary_text']

    logo_filename = settings.logo_filename if settings else None
    site_ids = _current_site_ids()
    if site_ids and len(site_ids) == 1:
        site = Site.query.get(site_ids[0])
        if site and site.logo_filename:
            logo_filename = site.logo_filename
    if logo_filename:
        branding['logo_url'] = url_for('branding_logo', filename=logo_filename)

    return branding


@app.context_processor
def inject_permission_helper():
    """Exposes can('people'|'devices'|'loaners'|'repairs') to every template, so
    nav links and buttons can hide themselves for users without that permission
    instead of just bouncing them back with an error after they click. Also
    exposes site-scope helpers so templates can hide site columns/filters for
    single-site users and gate Sites management to super admins.

    nav_overdue_count/nav_orphan_count power the small badges on the Admin ▾
    nav dropdown — only computed for a logged-in admin session (not kiosk-only
    visitors, who never see that dropdown), and only when the relevant
    permission is held, so this doesn't add queries to every page load."""
    nav_overdue_count = 0
    nav_orphan_count = 0
    if session.get('admin_logged_in'):
        if _has_permission('admin'):
            nav_overdue_count = len(_overdue_assignments(_current_site_ids()))
        if session.get('is_super_admin'):
            nav_orphan_count = Asset.query.filter_by(is_valid=False).count()
    return {
        'can': _has_permission,
        'is_super_admin': lambda: bool(session.get('is_super_admin')),
        'current_site_ids': _current_site_ids,
        'nav_overdue_count': nav_overdue_count,
        'nav_orphan_count': nav_orphan_count,
        'branding': _current_branding(),
    }


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
                session['is_super_admin'] = True
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
                session['is_super_admin'] = user.is_super_admin
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
    """
    Returns seconds remaining in session — used by the timeout warning UI.
    Deliberately read-only (does NOT refresh last_active): this is polled
    every 15s just to render the countdown, and if polling itself extended
    the session, an open-but-idle tab would keep the session alive forever.
    """
    if not session.get('admin_logged_in'):
        return jsonify({'authenticated': False})
    last_active = session.get('last_active', 0)
    elapsed = datetime.now(timezone.utc).timestamp() - last_active
    remaining = max(0, SESSION_TIMEOUT_MINUTES * 60 - int(elapsed))
    return jsonify({'authenticated': True, 'seconds_remaining': remaining})


@app.route('/api/admin/session_extend', methods=['POST'])
def session_extend():
    """
    Explicitly resets the sliding timeout — called only by the "Stay logged
    in" button. session_status() above intentionally can't do this (see its
    docstring), so a real click needs its own mutating endpoint.
    """
    if not _admin_session_active():
        return jsonify({'authenticated': False}), 401
    return jsonify({'authenticated': True, 'seconds_remaining': SESSION_TIMEOUT_MINUTES * 60})


# ─── Admin Panel ──────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_panel():
    site_ids = _current_site_ids()
    registry_count = _scope_registry(AssetRegistry.query, site_ids).count()
    people_count   = _scope_people(Person.query, site_ids).count()

    # Assets with no Asset row yet are implicitly 'available' (the default status).
    status_query = db.session.query(Asset.status, db.func.count(Asset.id))
    if site_ids is not None:
        status_query = status_query.join(AssetRegistry, AssetRegistry.asset_tag == Asset.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    explicit_counts = dict(status_query.group_by(Asset.status).all())
    non_available_explicit = sum(v for k, v in explicit_counts.items() if k != 'available')
    status_counts = {s: explicit_counts.get(s, 0) for s in ASSET_STATUSES}
    status_counts['available'] = registry_count - non_available_explicit
    overdue_count = len(_overdue_assignments(site_ids))
    warranty_expiring_count = _filter_registry_by_warranty(
        _scope_registry(AssetRegistry.query, site_ids), 'expiring').count()

    # Orphans have no site to attribute, and a per-site breakdown only makes
    # sense district-wide — both super-admin-only, along with the onboarding
    # banners below (a scoped site admin can't act on either anyway).
    orphan_count = None
    site_breakdown = None
    unassigned_devices = None
    fresh_install = None
    dev_secrets_in_use = None
    if site_ids is None:
        orphan_count = Asset.query.filter_by(is_valid=False).count()
        site_breakdown = [{
            'name': site.name,
            'registry_count': AssetRegistry.query.filter_by(site_id=site.id).count(),
            'people_count': Person.query.filter_by(site_id=site.id).count(),
        } for site in Site.query.order_by(Site.name).all()]
        unassigned_devices = AssetRegistry.query.filter(AssetRegistry.site_id.is_(None)).count()
        fresh_install = Site.query.first() is None
        # Same check that already logs a SECURITY WARNING to stdout at startup
        # (see the IS_PRODUCTION block above) — surfaced here too since that
        # log line is invisible unless someone is tailing container logs, and
        # a reused volume could already have a Site while still running on
        # dev-fallback secrets.
        dev_secrets_in_use = (
            app.secret_key == 'dev-secret-change-in-prod'
            or os.environ.get('ADMIN_PASSWORD') is None
        )

    google_loaner_autodisable_active = (
        GOOGLE_SYNC_ENABLED and GOOGLE_LOANER_AUTO_DISABLE_ENABLED
        and Site.query.filter_by(google_loaner_autodisable_enabled=True).first() is not None
    )
    branding_settings = BrandingSettings.query.get(1)
    branding_configured = bool(branding_settings and (branding_settings.primary_color_raw or branding_settings.logo_filename))

    return render_template('admin_panel.html',
                           registry_count=registry_count,
                           orphan_count=orphan_count,
                           people_count=people_count,
                           status_counts=status_counts,
                           overdue_count=overdue_count,
                           warranty_expiring_count=warranty_expiring_count,
                           site_breakdown=site_breakdown,
                           unassigned_devices=unassigned_devices,
                           fresh_install=fresh_install,
                           dev_secrets_in_use=dev_secrets_in_use,
                           email_enabled=EMAIL_ENABLED,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED,
                           google_loaner_autodisable_active=google_loaner_autodisable_active,
                           branding_configured=branding_configured)


@app.route('/admin/upload_csv', methods=['POST'])
@require_super_admin
def upload_csv():
    """
    Accepts a CSV with columns: asset_tag, serial_number (optional), description (optional).
    Completely replaces the registry (every site's devices, not just one) — so
    this is super-admin only. Site-scoped admins use /admin/registry/set_sites
    or the People-import-style upsert instead. Heals any orphaned Asset records afterwards.
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
        PURCHASE_DATE_COLS = ('purchase_date',)
        PURCHASE_COST_COLS = ('purchase_cost', 'cost')
        WARRANTY_COLS      = ('warranty_expiration', 'warranty')

        tag_col    = next((c for c in TAG_COLS    if c in normalized_headers), None)
        serial_col = next((c for c in SERIAL_COLS if c in normalized_headers), None)
        desc_col   = next((c for c in DESC_COLS   if c in normalized_headers), None)
        type_col   = next((c for c in TYPE_COLS   if c in normalized_headers), None)
        purchase_date_col = next((c for c in PURCHASE_DATE_COLS if c in normalized_headers), None)
        purchase_cost_col = next((c for c in PURCHASE_COST_COLS if c in normalized_headers), None)
        warranty_col       = next((c for c in WARRANTY_COLS if c in normalized_headers), None)

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
            purchase_date = _parse_date(row.get(purchase_date_col, '')) if purchase_date_col else None
            purchase_cost = _parse_money(row.get(purchase_cost_col, '')) if purchase_cost_col else None
            warranty_expiration = _parse_date(row.get(warranty_col, '')) if warranty_col else None

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
                purchase_date=purchase_date,
                purchase_cost=purchase_cost,
                warranty_expiration=warranty_expiration,
            ))
            imported += 1

        _log_activity('registry_csv_import', f'Replaced the asset registry via CSV upload: {imported} row(s) imported, {skipped} skipped.')
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


def _scope_registry(query, site_ids):
    """Applies the current session's site scope to an AssetRegistry query. None = unrestricted."""
    if site_ids is None:
        return query
    return query.filter(AssetRegistry.site_id.in_(site_ids))


def _parse_date(value):
    """Parses a 'YYYY-MM-DD' form field into a date, or None if blank/invalid."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _parse_money(value):
    """Parses a dollar-amount form field into a Decimal, or None if blank/invalid/negative."""
    value = (value or '').strip().lstrip('$')
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


WARRANTY_WARNING_DAYS = 60


def _filter_registry_by_warranty(query, mode):
    """mode='expiring': warranty runs out within WARRANTY_WARNING_DAYS. mode='expired': already past."""
    today = datetime.utcnow().date()
    if mode == 'expired':
        return query.filter(AssetRegistry.warranty_expiration.isnot(None),
                             AssetRegistry.warranty_expiration < today)
    horizon = today + timedelta(days=WARRANTY_WARNING_DAYS)
    return query.filter(AssetRegistry.warranty_expiration.isnot(None),
                         AssetRegistry.warranty_expiration >= today,
                         AssetRegistry.warranty_expiration <= horizon)


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
@require_permission('devices_manage')
def admin_registry_new():
    """Manually adds a single device. Leave asset_tag blank to self-assign a random 6-digit one."""
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    if request.method == 'POST':
        tag = request.form.get('asset_tag', '').strip()
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        site_id = request.form.get('site_id', type=int)
        purchase_date = _parse_date(request.form.get('purchase_date'))
        purchase_cost = _parse_money(request.form.get('purchase_cost'))
        warranty_expiration = _parse_date(request.form.get('warranty_expiration'))

        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form, sites=sites)

        if tag and AssetRegistry.query.filter_by(asset_tag=tag).first():
            flash(f'Asset tag "{tag}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form, sites=sites)

        if serial and AssetRegistry.query.filter_by(serial_number=serial).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form, sites=sites)

        if not tag:
            existing_tags = {t for (t,) in db.session.query(AssetRegistry.asset_tag).all()}
            tag = _generate_asset_tag(existing_tags)

        try:
            db.session.add(AssetRegistry(
                asset_tag=tag, serial_number=serial,
                description=description, device_type=device_type, site_id=site_id,
                purchase_date=purchase_date, purchase_cost=purchase_cost,
                warranty_expiration=warranty_expiration,
            ))
            _log_activity('device_add', f'Added device {tag} to the registry.', site_id=site_id)
            db.session.commit()
            # Heals a matching orphan scan record immediately, same effect as
            # upload_csv's heal_orphans() but without waiting for the next
            # bulk import — matters for the "Add to Registry" flow from the
            # Orphaned Records page.
            orphan = Asset.query.filter_by(asset_tag=tag, is_valid=False).first()
            if orphan:
                orphan.is_valid = True
                db.session.commit()
            flash(f'Added device {tag} to the registry.', 'success')
            return redirect(url_for('admin_asset_assign', asset_tag=tag))
        except IntegrityError as e:
            db.session.rollback()
            flash('Could not add device: that asset tag or serial number is already in use.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form, sites=sites)
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add device: {e}', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=request.form, sites=sites)

    prefill_tag = request.args.get('asset_tag', '').strip()
    prefill_form = {'asset_tag': prefill_tag} if prefill_tag else None
    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, form=prefill_form, sites=sites)


@app.route('/admin/registry/<string:asset_tag>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_edit(asset_tag):
    """Edits a device's attributes (serial, description, type, site). The
    asset_tag itself isn't editable here — it's the key used everywhere
    else (assignments, history, events), so renaming it is out of scope."""
    site_ids = _current_site_ids()
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first_or_404()
    sites = _sites_for_actor(site_ids)

    if request.method == 'POST':
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        site_id = request.form.get('site_id', type=int)
        purchase_date = _parse_date(request.form.get('purchase_date'))
        purchase_cost = _parse_money(request.form.get('purchase_cost'))
        warranty_expiration = _parse_date(request.form.get('warranty_expiration'))

        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, sites=sites)

        if serial and AssetRegistry.query.filter(AssetRegistry.serial_number == serial,
                                                   AssetRegistry.asset_tag != asset_tag).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, sites=sites)

        try:
            registry_row.serial_number = serial
            registry_row.description = description
            registry_row.device_type = device_type
            registry_row.site_id = site_id
            registry_row.purchase_date = purchase_date
            registry_row.purchase_cost = purchase_cost
            registry_row.warranty_expiration = warranty_expiration
            _log_activity('device_edit', f'Edited device {asset_tag}.', site_id=site_id)
            db.session.commit()
            flash(f'Updated {asset_tag}.', 'success')
            return redirect(url_for('admin_registry'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not update device: {e}', 'error')

    return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, sites=sites)


@app.route('/admin/registry/<string:asset_tag>/delete', methods=['POST'])
@require_permission('devices_manage')
def admin_registry_delete(asset_tag):
    """
    Permanently removes a device from the registry. Any current assignment is
    closed out first (same pattern as deleting a Person), and an open loaner
    checkout is auto-closed rather than left dangling. AssignmentHistory/Event/
    Incident/LoanerCheckout rows are untouched — they reference asset_tag as a
    plain string, not a foreign key, so history stays intact and readable.
    """
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    try:
        open_loaner = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
        if open_loaner:
            open_loaner.checked_in_at = datetime.utcnow()
            open_loaner.condition_notes = ((open_loaner.condition_notes + ' ') if open_loaner.condition_notes else '') + '[auto-closed: device deleted]'

        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if asset:
            _close_open_assignment(asset_tag, condition_in='Device deleted')
            db.session.delete(asset)

        registry_site_id = registry_row.site_id
        db.session.delete(registry_row)
        _log_activity('device_delete', f'Deleted device {asset_tag} from the registry.', site_id=registry_site_id)
        db.session.commit()
        flash(f'Deleted {asset_tag} from the registry.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete device: {e}', 'error')
    return redirect(url_for('admin_registry'))


@app.route('/admin/registry/set_sites', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_set_sites():
    """
    Non-destructive way to fill in devices' sites via CSV (upsert-by-asset_tag,
    same pattern as the People import) — for cleaning up whatever the one-time
    backfill couldn't infer, without needing the super-admin-only full replace.
    Columns: asset_tag, site.
    """
    results = None
    site_ids = _current_site_ids()

    if request.method == 'POST':
        if 'csv_file' not in request.files or not request.files['csv_file'].filename:
            flash('Choose a CSV file to upload.', 'error')
            return redirect(url_for('admin_registry_set_sites'))

        file = request.files['csv_file']
        if not file.filename.lower().endswith('.csv'):
            flash('File must be a .csv', 'error')
            return redirect(url_for('admin_registry_set_sites'))

        results = []
        try:
            content = file.stream.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
            fieldnames = [(f or '').strip().lower().replace(' ', '_') for f in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames

            if 'asset_tag' not in fieldnames or 'site' not in fieldnames:
                flash(f'CSV must have "asset_tag" and "site" columns. Found: {", ".join(fieldnames)}', 'error')
                return redirect(url_for('admin_registry_set_sites'))

            updated = skipped = 0
            for row in reader:
                tag = (row.get('asset_tag') or '').strip()
                site_name = (row.get('site') or '').strip()
                if not tag or not site_name:
                    skipped += 1
                    results.append({'row': tag or '(blank)', 'ok': False, 'message': 'Missing asset_tag or site.'})
                    continue

                registry_row = AssetRegistry.query.filter_by(asset_tag=tag).first()
                if not registry_row:
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': 'No device with that asset_tag.'})
                    continue
                if site_ids is not None and registry_row.site_id not in (site_ids + [None]):
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': 'That device belongs to a different site.'})
                    continue

                site_row = Site.query.filter(db.func.lower(Site.name) == site_name.lower()).first()
                if not site_row:
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': f'Unknown site "{site_name}".'})
                    continue
                if site_ids is not None and site_row.id not in site_ids:
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': f'"{site_name}" isn\'t one of your sites.'})
                    continue

                registry_row.site_id = site_row.id
                updated += 1
                results.append({'row': tag, 'ok': True, 'message': f'Set {tag} to {site_row.name}.'})

            _log_activity('registry_set_sites', f'Set sites via CSV for {updated} device(s), skipped {skipped}.')
            db.session.commit()
            flash(f'Updated {updated}, skipped {skipped} row(s).', 'success' if not skipped else 'info')
        except Exception as e:
            db.session.rollback()
            flash(f'Import failed: {e}', 'error')
            return redirect(url_for('admin_registry_set_sites'))

    return render_template('admin_registry_set_sites.html', results=results)


@app.route('/admin/registry')
@require_permission('devices')
def admin_registry():
    page          = request.args.get('page', 1, type=int)
    per_page      = 50
    site_ids      = _current_site_ids()
    query         = _scope_registry(AssetRegistry.query, site_ids).order_by(AssetRegistry.asset_tag)
    search        = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    type_filter   = request.args.get('device_type', '').strip()
    person_filter = request.args.get('person_id', '').strip()
    warranty_filter = request.args.get('warranty', '').strip()

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
    if warranty_filter in ('expiring', 'expired'):
        query = _filter_registry_by_warranty(query, warranty_filter)
    else:
        warranty_filter = ''

    person_filter_name = None
    if person_filter.isdigit():
        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_filter)).first()
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
                           warranty_filter=warranty_filter, today=datetime.utcnow().date(),
                           assets_by_tag=assets_by_tag)


@app.route('/admin/registry/export')
@require_permission('devices')
def admin_registry_export():
    """Exports the full asset list (not just the current page) as CSV, including
    live status and assignment — useful as a backup/reporting snapshot."""
    rows = _scope_registry(AssetRegistry.query, _current_site_ids()).order_by(AssetRegistry.asset_tag).all()
    assets_by_tag = {a.asset_tag: a for a in Asset.query.all()}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['asset_tag', 'serial_number', 'description', 'device_type', 'status', 'assigned_to', 'assigned_to_email',
                      'purchase_date', 'purchase_cost', 'warranty_expiration'])
    for row in rows:
        asset = assets_by_tag.get(row.asset_tag)
        status = asset.status if asset else 'available'
        person = asset.assigned_to if asset else None
        writer.writerow([
            row.asset_tag, row.serial_number or '', row.description or '', row.device_type,
            status, person.full_name if person else '', person.email if person else '',
            row.purchase_date.isoformat() if row.purchase_date else '',
            str(row.purchase_cost) if row.purchase_cost is not None else '',
            row.warranty_expiration.isoformat() if row.warranty_expiration else '',
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
    site_ids = _current_site_ids()
    if asset_tag and site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            asset_tag = None
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
    site_ids = _current_site_ids()

    if len(q) >= 2:
        people = _scope_people(Person.query, site_ids).filter(_person_search_filter(q)) \
            .order_by(Person.last_name, Person.first_name).limit(25).all()

        like = f'%{q}%'
        registry_rows = _scope_registry(AssetRegistry.query, site_ids).filter(
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
@require_super_admin
def admin_orphans():
    """Orphan scans have no matching AssetRegistry row, so there's nothing to
    attribute a site to — kept super-admin-only rather than guessing."""
    orphans = Asset.query.filter_by(is_valid=False).order_by(Asset.asset_tag).all()
    return render_template('admin_orphans.html', orphans=orphans)


@app.route('/admin/orphans/<string:asset_tag>/delete', methods=['POST'])
@require_super_admin
def admin_orphan_delete(asset_tag):
    """Permanently removes an orphan scan record — for a typo'd/mis-scanned
    tag that will never be a real device. A tag that IS a real device should
    go through 'Add to Registry' instead, which heals it rather than deleting it."""
    orphan = Asset.query.filter_by(asset_tag=asset_tag, is_valid=False).first_or_404()
    try:
        db.session.delete(orphan)
        _log_activity('orphan_delete', f'Deleted orphaned scan record {asset_tag}.')
        db.session.commit()
        flash(f'Deleted orphaned record {asset_tag}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete orphan: {e}', 'error')
    return redirect(url_for('admin_orphans'))


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
            Person.site.has(Site.name.ilike(like)),
            Person.external_id.ilike(like),
        ))
    return db.and_(*conditions)


def _scope_people(query, site_ids):
    """Applies the current session's site scope to a Person query. None = unrestricted."""
    if site_ids is None:
        return query
    return query.filter(Person.site_id.in_(site_ids))


@app.route('/admin/people')
@require_permission('people')
def admin_people():
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    show     = request.args.get('show', 'active')  # 'active' | 'inactive' | 'all'
    query    = _scope_people(Person.query, _current_site_ids()).order_by(Person.last_name, Person.first_name)
    if show == 'active':
        query = query.filter(Person.is_active.is_(True))
    elif show == 'inactive':
        query = query.filter(Person.is_active.is_(False))
    search   = request.args.get('q', '').strip()
    if search:
        query = query.filter(_person_search_filter(search))
    insurance_filter = request.args.get('insurance', '').strip()
    if insurance_filter == '1':
        query = query.filter(Person.insurance_opted_in.is_(True))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('admin_people.html', pagination=pagination, search=search, show=show,
                           insurance_filter=insurance_filter)


@app.route('/admin/people/search')
@kiosk_or_api_login_required
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

    matches = _scope_people(Person.query, _current_site_ids()).filter(
        Person.is_active.is_(True),
        _person_search_filter(q),
    ).order_by(Person.last_name, Person.first_name).limit(20).all()

    return jsonify([{
        'id': p.id, 'full_name': p.full_name, 'email': p.email,
        'site': p.site.name if p.site else None,
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
        'site_id':     request.form.get('site_id', type=int),
        'external_id': request.form.get('external_id', '').strip() or None,
        'grad_year':   int(grad_year_raw) if grad_year_raw.isdigit() else None,
        'insurance_opted_in': bool(request.form.get('insurance_opted_in')),
    }


def _validate_person_form(values, person_id=None, allowed_site_ids=None):
    """Returns an error message string, or None if the form values are valid.
    allowed_site_ids: None means unrestricted (super admin); otherwise the
    actor must pick one of their own sites — no blank/unassigned option."""
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
    if allowed_site_ids is not None:
        if not values['site_id']:
            return 'Choose a site.'
        if values['site_id'] not in allowed_site_ids:
            return 'You can only assign people to your own site(s).'
    return None


def _sites_for_actor(site_ids):
    """Sites the current session may pick from. None (super admin) = every site."""
    if site_ids is None:
        return Site.query.order_by(Site.name).all()
    return Site.query.filter(Site.id.in_(site_ids)).order_by(Site.name).all()


@app.route('/admin/people/new', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_new():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values, allowed_site_ids=site_ids)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=None, form=values,
                                    sites=_sites_for_actor(site_ids))

        try:
            person = Person(**values)
            db.session.add(person)
            _log_activity('person_add', f'Added {person.full_name}.', site_id=values.get('site_id'))
            db.session.commit()
            flash(f'Added {person.full_name}.', 'success')
            return redirect(url_for('admin_people'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add person: {e}', 'error')
            return render_template('admin_person_form.html', person=None, form=values,
                                    sites=_sites_for_actor(site_ids))

    return render_template('admin_person_form.html', person=None, form=None, sites=_sites_for_actor(site_ids))


@app.route('/admin/people/<int:person_id>/edit', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_edit(person_id):
    site_ids = _current_site_ids()
    person = _scope_people(Person.query, site_ids).filter_by(id=person_id).first_or_404()

    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values, person_id=person_id, allowed_site_ids=site_ids)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=person, form=values,
                                    sites=_sites_for_actor(site_ids))

        try:
            for field, value in values.items():
                setattr(person, field, value)
            _log_activity('person_edit', f'Edited {person.full_name}.', site_id=person.site_id)
            db.session.commit()
            flash(f'Updated {person.full_name}.', 'success')
            return redirect(url_for('admin_people'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not update person: {e}', 'error')
            return render_template('admin_person_form.html', person=person, form=values,
                                    sites=_sites_for_actor(site_ids))

    return render_template('admin_person_form.html', person=person, form=None, sites=_sites_for_actor(site_ids))


def _release_person_assets(person, condition_in):
    """Unassigns every asset currently held by a person. Shared by delete and
    the bulk graduate action. Returns the number of assets released."""
    affected_assets = Asset.query.filter_by(assigned_to_id=person.id).all()
    for asset in affected_assets:
        _close_open_assignment(asset.asset_tag, condition_in=condition_in)
        asset.assigned_to_id = None
        asset.status = 'available'
    return len(affected_assets)


@app.route('/admin/people/<int:person_id>/history')
@require_permission('people')
def admin_person_history(person_id):
    """
    Full chronological history for one person — every AssignmentHistory,
    LoanerCheckout, and Incident row they're linked to, newest first. The
    reverse direction of the combined Assignment & Loaner History card on
    the asset assign page (that page answers "who's had this device";
    this one answers "what has this person had").
    """
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()

    assignments = AssignmentHistory.query.filter_by(person_id=person.id) \
        .order_by(AssignmentHistory.assigned_at.desc()).all()
    loaner_checkouts = LoanerCheckout.query.filter_by(person_id=person.id) \
        .order_by(LoanerCheckout.checked_out_at.desc()).all()
    incidents = Incident.query.filter_by(person_id=person.id) \
        .order_by(Incident.created_at.desc()).all()

    combined_history = sorted(
        [{'kind': 'assign', 'asset_tag': h.asset_tag, 'timestamp': h.assigned_at,
          'ended_at': h.unassigned_at, 'acknowledged_by': h.acknowledged_by} for h in assignments] +
        [{'kind': 'loaner', 'asset_tag': l.asset_tag, 'timestamp': l.checked_out_at,
          'ended_at': l.checked_in_at, 'acknowledged_by': l.acknowledged_by} for l in loaner_checkouts] +
        [{'kind': 'incident', 'asset_tag': i.asset_tag, 'timestamp': i.created_at,
          'description': i.description, 'fee_charged': i.fee_charged,
          'fee_amount': i.fee_amount, 'paid_at': i.paid_at} for i in incidents],
        key=lambda row: row['timestamp'], reverse=True,
    )

    return render_template('admin_person_history.html', person=person, combined_history=combined_history)


@app.route('/admin/people/<int:person_id>/delete', methods=['POST'])
@require_permission('people')
def admin_person_delete(person_id):
    """
    Permanently deletes a person record. Any assets currently assigned to them
    are unassigned first, not blocked. AssignmentHistory/Incident/LoanerCheckout
    rows are kept (person_name is a snapshot) but their person_id link is
    cleared so the foreign key doesn't block the delete.

    For students leaving at graduation, prefer /admin/people/graduate instead —
    it archives (is_active=False) rather than deleting, so history/incidents
    stay fully linked. Use this route for genuine data-entry mistakes.
    """
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    unpaid_total = _person_unpaid_fee_total(person.id)
    person_name, person_site_id = person.full_name, person.site_id
    try:
        unassigned = _release_person_assets(person, condition_in='Person deleted')
        AssignmentHistory.query.filter_by(person_id=person.id).update({'person_id': None})
        Incident.query.filter_by(person_id=person.id).update({'person_id': None})
        LoanerCheckout.query.filter_by(person_id=person.id).update({'person_id': None})
        db.session.delete(person)
        _log_activity('person_delete', f'Deleted {person_name}.', site_id=person_site_id)
        db.session.commit()
        msg = f'Deleted {person.full_name}.'
        if unassigned:
            msg += f' Unassigned {unassigned} asset{"s" if unassigned != 1 else ""}.'
        if unpaid_total:
            msg += f' Note: they had ${unpaid_total:.2f} in unpaid fees on file.'
        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete person: {e}', 'error')
    return redirect(url_for('admin_people'))


@app.route('/admin/people/<int:person_id>/reactivate', methods=['POST'])
@require_permission('people')
def admin_person_reactivate(person_id):
    """Undoes an accidental graduate/archive — marks a person active again."""
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    person.is_active = True
    _log_activity('person_reactivate', f'Reactivated {person.full_name}.', site_id=person.site_id)
    db.session.commit()
    flash(f'Reactivated {person.full_name}.', 'success')
    return redirect(url_for('admin_people', show='inactive'))


def _parse_bool_csv(value):
    """Parses a lenient boolean CSV cell. Returns True/False, or None if the
    cell is blank/missing — None means 'leave the existing value alone' on an
    upsert, distinct from an explicit false which clears a previously-true flag."""
    value = (value or '').strip().lower()
    if not value:
        return None
    return value in ('true', 'yes', 'y', '1')


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
        site_ids = _current_site_ids()
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
                site_name  = clean(row.get('site'))
                grad_year_raw = clean(row.get('grad_year') or row.get('graduation_year'))
                grad_year  = int(grad_year_raw) if grad_year_raw and grad_year_raw.isdigit() else None
                insurance_opted_in = _parse_bool_csv(row.get('insurance') or row.get('insurance_opted_in'))

                if not first_name or not last_name or not email:
                    skipped += 1
                    results.append({'row': email or external_id or '(blank)', 'ok': False,
                                    'message': 'Missing first_name, last_name, or email.'})
                    continue

                site_id = None
                if site_name:
                    site_row = Site.query.filter(db.func.lower(Site.name) == site_name.lower()).first()
                    if not site_row:
                        skipped += 1
                        results.append({'row': email, 'ok': False,
                                        'message': f'Unknown site "{site_name}" — add it under Sites first.'})
                        continue
                    site_id = site_row.id
                    if site_ids is not None and site_id not in site_ids:
                        skipped += 1
                        results.append({'row': email, 'ok': False,
                                        'message': f'"{site_name}" isn\'t one of your sites.'})
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

                if person and site_ids is not None and person.site_id not in site_ids:
                    skipped += 1
                    results.append({'row': email, 'ok': False,
                                    'message': f'{email} belongs to a different site — not yours to update.'})
                    continue

                if not person and site_ids is not None and site_id is None:
                    skipped += 1
                    results.append({'row': email, 'ok': False,
                                    'message': 'New person needs a "site" column value (one of your own sites).'})
                    continue

                if person:
                    person.first_name = first_name
                    person.last_name  = last_name
                    person.email      = email
                    if external_id:  person.external_id = external_id
                    if role:         person.role = role
                    if department:   person.department = department
                    if site_id:      person.site_id = site_id
                    if grad_year:    person.grad_year = grad_year
                    if insurance_opted_in is not None: person.insurance_opted_in = insurance_opted_in
                    updated += 1
                    results.append({'row': email, 'ok': True, 'message': f'Updated {person.full_name}.'})
                else:
                    person = Person(
                        first_name=first_name, last_name=last_name, email=email,
                        external_id=external_id, role=role or 'staff',
                        department=department, site_id=site_id, grad_year=grad_year,
                        insurance_opted_in=bool(insurance_opted_in),
                    )
                    db.session.add(person)
                    created += 1
                    results.append({'row': email, 'ok': True, 'message': f'Created {first_name} {last_name}.'})

            _log_activity('people_csv_import', f'Imported people via CSV: {created} created, {updated} updated, {skipped} skipped.')
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

        students = _scope_people(Person.query, _current_site_ids()) \
            .filter_by(role='student', grad_year=grad_year, is_active=True).all()
        if not students:
            flash(f'No active students found with graduation year {grad_year}.', 'info')
            return redirect(url_for('admin_people_graduate'))

        unassigned_total = 0
        fees_total = Decimal('0')
        for student in students:
            unassigned_total += _release_person_assets(student, condition_in='Graduated')
            fees_total += _person_unpaid_fee_total(student.id)
            student.is_active = False
        _log_activity('people_graduate', f'Graduated {len(students)} student(s), class of {grad_year}.')
        db.session.commit()

        msg = (f'Graduated {len(students)} student{"s" if len(students) != 1 else ""} '
               f'(class of {grad_year}). Unassigned {unassigned_total} device'
               f'{"s" if unassigned_total != 1 else ""}.')
        if fees_total:
            msg += f' Note: ${fees_total:.2f} in unpaid fees across this class.'
        flash(msg, 'success')
        return redirect(url_for('admin_people', show='inactive'))

    site_ids = _current_site_ids()
    counts_query = db.session.query(Person.grad_year, db.func.count(Person.id)) \
        .filter(Person.role == 'student', Person.is_active.is_(True), Person.grad_year.isnot(None))
    if site_ids is not None:
        counts_query = counts_query.filter(Person.site_id.in_(site_ids))
    grad_year_counts = dict(counts_query.group_by(Person.grad_year).order_by(Person.grad_year).all())
    return render_template('admin_people_graduate.html', grad_year_counts=grad_year_counts)


# ─── Asset Assignment ─────────────────────────────────────────────────────────

def _close_open_assignment(asset_tag, condition_in=None):
    """Closes the current open AssignmentHistory row for an asset_tag, if any."""
    open_row = AssignmentHistory.query.filter_by(asset_tag=asset_tag, unassigned_at=None).first()
    if open_row:
        open_row.unassigned_at = datetime.utcnow()
        open_row.condition_in = condition_in


def _assign_asset_to_person(asset_tag, person, condition_out=None, due_date=None, acknowledged_by=None):
    """
    Shared assign logic used by both the single assign form and bulk assign.
    The asset_tag must already exist in the registry (caller's responsibility
    to check) — the live Asset row is created here if scanning hasn't made one yet.

    acknowledged_by MUST stay the last parameter — both callers below invoke
    this positionally, so inserting a new param earlier would silently shift
    due_date into the wrong argument with no error.

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
            condition_out=condition_out, due_date=due_date, acknowledged_by=acknowledged_by,
        ))
        asset.assigned_to_id = person.id
        asset.status = 'assigned'
        registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        _log_activity('device_assign', f'Assigned {asset_tag} to {person.full_name}.',
                       site_id=registry_row.site_id if registry_row else None)
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
    site_ids = _current_site_ids()
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first_or_404()
    asset = Asset.query.filter_by(asset_tag=asset_tag).first()

    if request.method == 'POST':
        person_id = request.form.get('person_id', type=int)
        condition_out = request.form.get('condition_out', '').strip() or None
        due_date_str = request.form.get('due_date', '').strip()
        acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
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

        person = _scope_people(Person.query, site_ids).filter_by(id=person_id).first_or_404()
        status, message = _assign_asset_to_person(asset_tag, person, condition_out, due_date,
                                                    acknowledged_by=acknowledged_by)
        if status == 'assigned' and registry_row.site_id and person.site_id and registry_row.site_id != person.site_id:
            message += ' Note: this device and person are at different sites.'
        flash(message, 'info' if status == 'already' else ('success' if status == 'assigned' else 'error'))
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    has_people = Person.query.first() is not None
    history = AssignmentHistory.query.filter_by(asset_tag=asset_tag) \
        .order_by(AssignmentHistory.assigned_at.desc()).all()
    loaner_checkouts = LoanerCheckout.query.filter_by(asset_tag=asset_tag) \
        .order_by(LoanerCheckout.checked_out_at.desc()).all()
    # Normalized into a common shape and interleaved chronologically — an
    # asset that's ever spent time in the loaner pool otherwise had an
    # incomplete history here (assign-only), even though its loaner checkouts
    # are tracked in a separate table.
    combined_history = sorted(
        [{
            'kind': 'assign', 'person_name': h.person_name, 'started_at': h.assigned_at,
            'ended_at': h.unassigned_at, 'due_date': h.due_date, 'acknowledged_by': h.acknowledged_by,
            'notes': ' '.join(filter(None, [
                f'Out: {h.condition_out}' if h.condition_out else None,
                f'In: {h.condition_in}' if h.condition_in else None,
            ])) or None,
        } for h in history] +
        [{
            'kind': 'loaner', 'person_name': l.person_name, 'started_at': l.checked_out_at,
            'ended_at': l.checked_in_at, 'due_date': l.due_date, 'acknowledged_by': l.acknowledged_by,
            'notes': l.condition_notes,
        } for l in loaner_checkouts],
        key=lambda row: row['started_at'], reverse=True,
    )
    events = Event.query.filter_by(asset_tag=asset_tag) \
        .order_by(Event.timestamp.desc()).limit(20).all()
    incidents = Incident.query.filter_by(asset_tag=asset_tag) \
        .order_by(Incident.created_at.desc()).all()

    current_person_incident_count = None
    if asset and asset.assigned_to_id:
        current_person_incident_count = Incident.query.filter_by(person_id=asset.assigned_to_id).count()

    open_repair = Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first()
    closed_repairs = Repair.query.filter(Repair.asset_tag == asset_tag, Repair.returned_at.isnot(None)) \
        .order_by(Repair.returned_at.desc()).all()

    return render_template('admin_assign.html', registry_row=registry_row, asset=asset, has_people=has_people,
                           history=history, combined_history=combined_history, events=events, incidents=incidents,
                           current_person_incident_count=current_person_incident_count,
                           open_repair=open_repair, closed_repairs=closed_repairs, repair_outcomes=REPAIR_OUTCOMES,
                           asset_statuses=ASSET_STATUSES,
                           now=datetime.utcnow().date(), google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/bulk_assign', methods=['GET', 'POST'])
@require_permission('devices')
def admin_bulk_assign():
    """
    Bulk-assigns a whole roster in one upload — the start-of-year "hand out
    every Chromebook" workflow. CSV columns: asset_tag, email, due_date (optional).
    People must already exist (use /admin/people or import them first); this
    intentionally does not auto-create people from a typo'd email.
    """
    results = None
    site_ids = _current_site_ids()

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

                registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
                if not registry_row:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'Asset tag not found in registry.'})
                    continue
                if site_ids is not None and registry_row.site_id not in site_ids:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'That device belongs to a different site.'})
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
                if site_ids is not None and person.site_id not in site_ids:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': f'{person.full_name} belongs to a different site.'})
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
                if status == 'assigned' and registry_row.site_id and person.site_id and registry_row.site_id != person.site_id:
                    message += ' (different sites)'
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

    site_ids = _current_site_ids()

    if order_mode == 'scan':
        # First-scan time per tag today (or since the given date) gives the
        # exact physical walk order; re-scanning a tag doesn't move it later.
        scan_times = (db.session.query(AuditScan.asset_tag, db.func.min(AuditScan.scanned_at))
                      .filter(AuditScan.scanned_at >= since)
                      .group_by(AuditScan.asset_tag)
                      .order_by(db.func.min(AuditScan.scanned_at))
                      .all())
        ordered_tags = [tag for tag, _ in scan_times]
        registry_by_tag = {r.asset_tag: r for r in _scope_registry(AssetRegistry.query, site_ids).filter(
            AssetRegistry.asset_tag.in_(ordered_tags)
        )}
        rows = [registry_by_tag[t] for t in ordered_tags if t in registry_by_tag]
        if type_filter:
            rows = [r for r in rows if r.device_type == type_filter]
    else:
        query = _scope_registry(AssetRegistry.query, site_ids).order_by(AssetRegistry.asset_tag)
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
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    asset = Asset.query.filter_by(asset_tag=asset_tag).first_or_404()
    condition_in = request.form.get('condition_in', '').strip() or None
    try:
        _close_open_assignment(asset_tag, condition_in=condition_in)
        asset.assigned_to_id = None
        asset.status = 'available'
        _log_activity('device_unassign', f'Unassigned {asset_tag}.', site_id=registry_row.site_id)
        db.session.commit()
        flash(f'Unassigned {asset_tag}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not unassign asset: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/assets/<string:asset_tag>/status', methods=['POST'])
@require_permission('devices')
def admin_asset_status(asset_tag):
    """Manual status override, independent of assignment. 'repair' can't be set
    directly here — use Send to Repair below, which also creates the tracking
    record. If a status change here bypasses an open Repair some other way,
    that Repair is auto-closed rather than left dangling (same precedent as
    admin_registry_delete auto-closing an open LoanerCheckout)."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    new_status = request.form.get('status', '')
    if new_status not in ASSET_STATUSES:
        flash('Invalid status.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))
    if new_status == 'repair':
        flash('Use "Send to Repair" below to mark a device as in repair — it keeps a tracking record.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    try:
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.status = new_status
        open_repair = Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first()
        if open_repair:
            open_repair.returned_at = datetime.utcnow()
            open_repair.notes = ((open_repair.notes + ' ') if open_repair.notes else '') + '[auto-closed: status changed manually]'
        _log_activity('device_status', f'Set {asset_tag} status to {new_status}.', site_id=registry_row.site_id)
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
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()

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
    except LookupError as e:
        flash(str(e), 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Google sync failed: {e}', 'error')

    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/google_setup')
@require_super_admin
def admin_google_setup():
    """
    Guided setup checklist for Google Workspace sync. The Google Cloud
    Console / Workspace Admin steps genuinely can't be automated from here —
    Google requires a human super admin to authorize Domain-wide Delegation
    in the Admin console, and no API exists for that step (GAM can't
    automate it either) — so this is a checklist with direct links plus a
    live connectivity test at the end, not a wizard that does the work for you.
    """
    return render_template('admin_google_setup.html',
                           google_sync_enabled=GOOGLE_SYNC_ENABLED,
                           google_loaner_autodisable_enabled=GOOGLE_LOANER_AUTO_DISABLE_ENABLED,
                           service_account_file=GOOGLE_SERVICE_ACCOUNT_FILE,
                           impersonate_email=GOOGLE_ADMIN_IMPERSONATE_EMAIL)


@app.route('/admin/google_setup/test', methods=['POST'])
@require_super_admin
def admin_google_setup_test():
    """
    A live round-trip against the Admin SDK Directory API — the only way to
    actually confirm the Google Cloud Console + Workspace Admin steps worked.
    GOOGLE_SYNC_ENABLED (used everywhere else) is just an env-var-presence
    check, not proof the credentials/delegation are actually valid.
    """
    if not GOOGLE_SYNC_ENABLED:
        flash('Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_ADMIN_IMPERSONATE_EMAIL in .env and restart before testing.', 'error')
        return redirect(url_for('admin_google_setup'))
    try:
        service = _google_directory_service([GOOGLE_SCOPE_READONLY])
        response = service.chromeosdevices().list(customerId='my_customer', maxResults=1).execute()
        if response.get('chromeosdevices'):
            flash('Connected to Google Workspace successfully — found at least one Chrome device on file.', 'success')
        else:
            flash('Connected to Google Workspace successfully, but no Chrome devices were found — '
                  'double-check GOOGLE_ADMIN_IMPERSONATE_EMAIL is a real super admin on this domain.', 'info')
    except Exception as e:
        flash(f'Connection failed: {e}', 'error')
    return redirect(url_for('admin_google_setup'))


# ─── Kiosk Devices ──────────────────────────────────────────────────────────────

@app.route('/admin/kiosk')
@require_permission('admin')
def admin_kiosk():
    site_ids = _current_site_ids()
    query = KioskDevice.query
    if site_ids is not None:
        query = query.filter(KioskDevice.site_id.in_(site_ids))
    devices = query.order_by(KioskDevice.created_at.desc()).all()
    token = request.cookies.get('kiosk_token')
    current_device = KioskDevice.query.filter_by(token=token).first() if token else None
    return render_template('admin_kiosk.html', devices=devices, current_device=current_device,
                           sites=_sites_for_actor(site_ids))


@app.route('/admin/kiosk/enable', methods=['POST'])
@require_permission('admin')
def admin_kiosk_enable():
    """Enrolls the device making this request (i.e. the kiosk itself) via a long-lived cookie."""
    label = request.form.get('label', '').strip() or None
    site_id = request.form.get('site_id', type=int)
    site_ids = _current_site_ids()
    if site_ids is not None and (not site_id or site_id not in site_ids):
        flash('Choose one of your own sites for this kiosk.', 'error')
        return redirect(url_for('admin_kiosk'))
    if site_ids is None and not site_id:
        flash('Choose a site for this kiosk.', 'error')
        return redirect(url_for('admin_kiosk'))
    token = secrets.token_urlsafe(32)
    try:
        db.session.add(KioskDevice(token=token, label=label, site_id=site_id))
        _log_activity('kiosk_enroll', f'Enrolled kiosk device "{label or token[:8]}".', site_id=site_id)
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
    site_ids = _current_site_ids()
    query = KioskDevice.query
    if site_ids is not None:
        query = query.filter(KioskDevice.site_id.in_(site_ids))
    device = query.filter_by(id=device_id).first_or_404()
    try:
        label = device.label or 'that device'
        device_site_id = device.site_id
        db.session.delete(device)
        _log_activity('kiosk_revoke', f'Revoked kiosk access for {label}.', site_id=device_site_id)
        db.session.commit()
        flash(f'Revoked kiosk access for {label}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not revoke device: {e}', 'error')
    return redirect(url_for('admin_kiosk'))


# ─── Users & Permissions ──────────────────────────────────────────────────────

def _user_form_permissions(actor_is_admin):
    """
    Reads permission checkboxes from the form. is_admin is only included (and
    therefore only ever settable) when the acting session is itself is_admin —
    otherwise a manage_users-only actor could create/edit a user into a proxy
    full-admin account. Omitting the key leaves the target's existing is_admin
    value untouched on edit, and defaults to False on create.
    """
    perms = {
        'can_people':  bool(request.form.get('can_people')),
        'can_devices': bool(request.form.get('can_devices')),
        'can_devices_manage': bool(request.form.get('can_devices_manage')),
        'can_loaners': bool(request.form.get('can_loaners')),
        'can_loaner_checkinout': bool(request.form.get('can_loaner_checkinout')),
        'can_checkinout': bool(request.form.get('can_checkinout')),
        'can_repairs': bool(request.form.get('can_repairs')),
        'can_manage_users': bool(request.form.get('can_manage_users')),
    }
    if actor_is_admin:
        perms['is_admin'] = bool(request.form.get('is_admin'))
    return perms


def _scope_users(query, site_ids):
    """A site-scoped admin only sees/manages users who share at least one of their sites."""
    if site_ids is None:
        return query
    return query.filter(User.sites.any(Site.id.in_(site_ids)))


@app.route('/admin/users')
@require_permission('manage_users')
def admin_users():
    users = _scope_users(User.query, _current_site_ids()).order_by(User.username).all()
    return render_template('admin_users.html', users=users)


@app.route('/admin/users/new', methods=['GET', 'POST'])
@require_permission('manage_users')
def admin_user_new():
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('admin_user_form.html', user=None, sites=sites)
        if User.query.filter(db.func.lower(User.username) == username.lower()).first():
            flash(f'Username "{username}" is already taken.', 'error')
            return render_template('admin_user_form.html', user=None, sites=sites)

        # A site-scoped admin can only grant their own sites, and only a super
        # admin can create another super admin — never trust the posted flag alone.
        wants_super_admin = site_ids is None and bool(request.form.get('is_super_admin'))
        selected_site_ids = request.form.getlist('site_ids', type=int)
        if site_ids is not None:
            selected_site_ids = [s for s in selected_site_ids if s in site_ids]

        user = User(username=username, password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
                     is_super_admin=wants_super_admin, **_user_form_permissions(bool(session.get('is_admin'))))
        if not wants_super_admin:
            user.sites = Site.query.filter(Site.id.in_(selected_site_ids)).all()
        db.session.add(user)
        _log_activity('user_add', f'Created user "{username}".')
        db.session.commit()
        flash(f'Created user "{username}".', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin_user_form.html', user=None, sites=sites)


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@require_permission('manage_users')
def admin_user_edit(user_id):
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    user = _scope_users(User.query, site_ids).filter_by(id=user_id).first_or_404()
    if request.method == 'POST':
        new_password = request.form.get('password', '')
        for field, value in _user_form_permissions(bool(session.get('is_admin'))).items():
            setattr(user, field, value)
        user.is_active = bool(request.form.get('is_active'))

        if site_ids is None:  # only a super admin can change super-admin status
            user.is_super_admin = bool(request.form.get('is_super_admin'))

        if not user.is_super_admin:
            selected_site_ids = request.form.getlist('site_ids', type=int)
            if site_ids is not None:
                selected_site_ids = [s for s in selected_site_ids if s in site_ids]
            user.sites = Site.query.filter(Site.id.in_(selected_site_ids)).all()

        if new_password:
            user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        _log_activity('user_edit', f'Edited user "{user.username}".')
        db.session.commit()
        flash(f'Updated user "{user.username}".', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin_user_form.html', user=user, sites=sites)


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@require_permission('manage_users')
def admin_user_delete(user_id):
    user = _scope_users(User.query, _current_site_ids()).filter_by(id=user_id).first_or_404()
    username = user.username
    db.session.delete(user)
    _log_activity('user_delete', f'Deleted user "{username}".')
    db.session.commit()
    flash(f'Deleted user "{username}".', 'success')
    return redirect(url_for('admin_users'))


# ─── Branding ─────────────────────────────────────────────────────────────────

def _get_branding_settings():
    """Get-or-create the single BrandingSettings row (always id=1)."""
    settings = BrandingSettings.query.get(1)
    if not settings:
        settings = BrandingSettings(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings


@app.route('/branding/logo/<path:filename>')
def branding_logo(filename):
    """
    Unauthenticated on purpose: the login page and kiosk-facing pages need
    to show a logo before any auth happens. filename is always one we
    generated ourselves (see _save_branding_logo's random suffix) — never
    user-supplied at save time — and send_from_directory guards against
    path traversal regardless.
    """
    return send_from_directory(BRANDING_UPLOAD_DIR, filename, max_age=86400)


@app.route('/admin/branding/preview')
@require_super_admin
def admin_branding_preview():
    """Powers the live swatch preview on the settings page as the admin
    moves the color picker — same generate_palette() the save route uses,
    just not persisted, so what they see is exactly what they'll get."""
    color = request.args.get('color', '')
    if not _HEX_RE.match(color):
        return jsonify({'error': 'invalid color'}), 400
    return jsonify(generate_palette(color))


@app.route('/admin/branding', methods=['GET', 'POST'])
@require_super_admin
def admin_branding():
    settings = _get_branding_settings()

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'reset_color':
            settings.primary_color_raw = None
            settings.primary_color = None
            settings.accent_dim_color = None
            settings.accent_text_color = None
            settings.secondary_color = None
            settings.secondary_text_color = None
            settings.tertiary_color = None
            settings.tertiary_text_color = None
            _log_activity('branding_edit', 'Reset branding colors to the built-in default.')
            db.session.commit()
            flash('Colors reset to the built-in default.', 'success')
            return redirect(url_for('admin_branding'))

        if action == 'remove_logo':
            _delete_branding_logo(settings.logo_filename)
            settings.logo_filename = None
            _log_activity('branding_edit', 'Removed the district-wide default logo.')
            db.session.commit()
            flash('Logo removed.', 'success')
            return redirect(url_for('admin_branding'))

        app_name = request.form.get('app_name', '').strip()
        primary_color = request.form.get('primary_color', '').strip()

        if not _HEX_RE.match(primary_color):
            flash('Primary color must be a valid hex color (e.g. #c8102e).', 'error')
            return render_template('admin_branding.html', settings=settings)

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), 'global')
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_branding.html', settings=settings)

        settings.app_name = app_name or None
        if new_logo:
            _delete_branding_logo(settings.logo_filename)
            settings.logo_filename = new_logo

        settings.primary_color_raw = primary_color
        palette = generate_palette(primary_color)
        settings.primary_color        = palette['accent']
        settings.accent_dim_color     = palette['accent_dim']
        settings.accent_text_color    = palette['accent_text']
        settings.secondary_color      = palette['secondary']
        settings.secondary_text_color = palette['secondary_text']
        settings.tertiary_color       = palette['tertiary']
        settings.tertiary_text_color  = palette['tertiary_text']

        _log_activity('branding_edit', 'Updated app branding (logo/colors).')
        db.session.commit()
        flash('Branding updated.', 'success')
        return redirect(url_for('admin_branding'))

    return render_template('admin_branding.html', settings=settings)


# ─── Sites ────────────────────────────────────────────────────────────────────

@app.route('/admin/sites')
@require_super_admin
def admin_sites():
    sites = Site.query.order_by(Site.name).all()
    return render_template('admin_sites.html', sites=sites)


@app.route('/admin/sites/new', methods=['GET', 'POST'])
@require_super_admin
def admin_site_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Site name is required.', 'error')
            return render_template('admin_site_form.html', site=None, form=request.form)
        if Site.query.filter(db.func.lower(Site.name) == name.lower()).first():
            flash(f'A site named "{name}" already exists.', 'error')
            return render_template('admin_site_form.html', site=None, form=request.form)

        site = Site(name=name, google_loaner_autodisable_enabled=bool(request.form.get('google_loaner_autodisable_enabled')))
        db.session.add(site)
        db.session.flush()  # assigns site.id, used as the logo filename prefix below

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), f'site{site.id}')
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
            return render_template('admin_site_form.html', site=None, form=request.form)
        if new_logo:
            site.logo_filename = new_logo

        _log_activity('site_add', f'Added site "{name}".')
        db.session.commit()
        flash(f'Added site "{name}".', 'success')
        return redirect(url_for('admin_sites'))

    return render_template('admin_site_form.html', site=None, form=None)


@app.route('/admin/sites/<int:site_id>/edit', methods=['GET', 'POST'])
@require_super_admin
def admin_site_edit(site_id):
    site = Site.query.get_or_404(site_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Site name is required.', 'error')
            return render_template('admin_site_form.html', site=site, form=request.form)
        dupe = Site.query.filter(db.func.lower(Site.name) == name.lower(), Site.id != site_id).first()
        if dupe:
            flash(f'A site named "{name}" already exists.', 'error')
            return render_template('admin_site_form.html', site=site, form=request.form)

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), f'site{site.id}')
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_site_form.html', site=site, form=request.form)

        site.name = name
        site.google_loaner_autodisable_enabled = bool(request.form.get('google_loaner_autodisable_enabled'))
        if new_logo:
            _delete_branding_logo(site.logo_filename)
            site.logo_filename = new_logo
        elif request.form.get('remove_logo'):
            _delete_branding_logo(site.logo_filename)
            site.logo_filename = None

        _log_activity('site_edit', f'Edited site "{name}".', site_id=site.id)
        db.session.commit()
        flash(f'Updated site "{name}".', 'success')
        return redirect(url_for('admin_sites'))

    return render_template('admin_site_form.html', site=site, form=None)


@app.route('/admin/sites/<int:site_id>/delete', methods=['POST'])
@require_super_admin
def admin_site_delete(site_id):
    site = Site.query.get_or_404(site_id)
    in_use = (
        Person.query.filter_by(site_id=site.id).first()
        or AssetRegistry.query.filter_by(site_id=site.id).first()
        or KioskDevice.query.filter_by(site_id=site.id).first()
    )
    if in_use:
        flash(f'Can\'t delete "{site.name}" — it still has people, devices, or kiosks assigned to it.', 'error')
        return redirect(url_for('admin_sites'))
    try:
        site_name = site.name
        site_logo = site.logo_filename
        UserSite.query.filter_by(site_id=site.id).delete()
        db.session.delete(site)
        _log_activity('site_delete', f'Deleted site "{site_name}".')
        db.session.commit()
        _delete_branding_logo(site_logo)
        flash(f'Deleted site "{site.name}".', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete site: {e}', 'error')
    return redirect(url_for('admin_sites'))


# ─── Overdue Reminders ────────────────────────────────────────────────────────

def _overdue_assignments(site_ids=None):
    """Open assignments (not yet returned) whose due_date has passed.
    site_ids=None means unrestricted (e.g. the background job)."""
    today = datetime.utcnow().date()
    query = AssignmentHistory.query.filter(
        AssignmentHistory.unassigned_at.is_(None),
        AssignmentHistory.due_date.isnot(None),
        AssignmentHistory.due_date < today,
    )
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == AssignmentHistory.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    return query.order_by(AssignmentHistory.due_date).all()


@app.route('/admin/reminders')
@require_permission('admin')
def admin_reminders():
    overdue = _overdue_assignments(_current_site_ids())
    today = datetime.utcnow().date()
    return render_template('admin_reminders.html', overdue=overdue, today=today,
                           email_enabled=EMAIL_ENABLED)


@app.route('/admin/reminders/send', methods=['POST'])
@require_permission('admin')
def admin_reminders_send():
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_FROM_EMAIL (and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth) in .env to enable it.', 'info')
        return redirect(url_for('admin_reminders'))

    overdue = _overdue_assignments(_current_site_ids())
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

    if sent:
        _log_activity('reminders_send', f'Manually sent {sent} overdue-assignment reminder(s).')
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
@require_permission('devices_manage')
def admin_toggle_loaner(asset_tag):
    row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    row.is_loaner = not row.is_loaner
    _log_activity('loaner_toggle', f'{asset_tag} is {"now" if row.is_loaner else "no longer"} in the loaner pool.', site_id=row.site_id)
    db.session.commit()
    flash(f'{asset_tag} is {"now" if row.is_loaner else "no longer"} in the loaner pool.', 'success')
    return redirect(request.referrer or url_for('admin_loaners'))


def _overdue_loaners(site_ids=None):
    """Open loaner checkouts (not yet returned) whose due_date has passed.
    site_ids=None means unrestricted (e.g. the background job)."""
    today = datetime.utcnow().date()
    query = LoanerCheckout.query.filter(
        LoanerCheckout.checked_in_at.is_(None),
        LoanerCheckout.due_date.isnot(None),
        LoanerCheckout.due_date < today,
    )
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    return query.order_by(LoanerCheckout.due_date).all()


def _send_overdue_loaner_reminders(site_ids=None):
    """
    Emails anyone with an overdue loaner. Safe to call repeatedly (e.g. from a
    background loop) — reminder_sent_at gates re-sending to once per
    LOANER_REMINDER_RESEND_HOURS, so it won't spam the same student hourly.
    site_ids=None (the background loop's case) means every site.
    Returns (sent, failed, skipped) counts.
    """
    if not EMAIL_ENABLED:
        return 0, 0, 0

    sent = failed = skipped = 0
    now = datetime.utcnow()
    for row in _overdue_loaners(site_ids):
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

    if sent:
        _log_activity('reminders_send', f'Sent {sent} overdue-loaner reminder(s).')
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
    site_ids = _current_site_ids()
    loaner_rows = _scope_registry(AssetRegistry.query, site_ids).filter_by(is_loaner=True) \
        .order_by(AssetRegistry.asset_tag).all()
    tags = [r.asset_tag for r in loaner_rows]
    open_checkouts = {
        c.asset_tag: c for c in LoanerCheckout.query.filter(
            LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.is_(None)
        )
    }
    overdue_count = len(_overdue_loaners(site_ids))
    return render_template('admin_loaners.html', loaner_rows=loaner_rows, open_checkouts=open_checkouts,
                           overdue_count=overdue_count, email_enabled=EMAIL_ENABLED,
                           today=datetime.utcnow().date())


@app.route('/admin/collection')
@login_required
def admin_collection():
    """
    "Who still has what" — currently-assigned devices unioned with currently-
    checked-out loaners, site-scoped, in one combined view. Useful for
    end-of-year collection: instead of cross-referencing the registry and
    loaner pages separately, see the whole outstanding list at once.

    Gated like admin_panel.html's dashboard cards — each half only shows for
    a session with that specific permission, rather than requiring both
    'devices' and 'loaners' just to see either one.
    """
    if not (_has_permission('devices') or _has_permission('loaners')):
        flash('Your account doesn\'t have permission to access that page.', 'error')
        return redirect(url_for('admin_panel'))

    site_ids = _current_site_ids()

    assigned_rows = []
    if _has_permission('devices'):
        registry_rows = _filter_registry_by_status(
            _scope_registry(AssetRegistry.query, site_ids), 'assigned'
        ).order_by(AssetRegistry.asset_tag).all()
        tags = [r.asset_tag for r in registry_rows]
        assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(tags))}
        assigned_rows = [(r, assets_by_tag.get(r.asset_tag)) for r in registry_rows]

    open_loaners = []
    if _has_permission('loaners'):
        loaner_query = LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None))
        if site_ids is not None:
            loaner_query = loaner_query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
                .filter(AssetRegistry.site_id.in_(site_ids))
        open_loaners = loaner_query.order_by(LoanerCheckout.checked_out_at).all()

    return render_template('admin_collection.html', assigned_rows=assigned_rows, open_loaners=open_loaners,
                           now=datetime.utcnow().date())


@app.route('/admin/loaners/send_reminders', methods=['POST'])
@require_permission('loaners')
def admin_loaners_send_reminders():
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_FROM_EMAIL (and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth) in .env to enable it.', 'info')
        return redirect(url_for('admin_loaners'))
    sent, failed, skipped = _send_overdue_loaner_reminders(_current_site_ids())
    msg = f'Sent {sent} reminder{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'info')
    return redirect(url_for('admin_loaners'))


def _checkout_loaner(asset_tag, person, due_date=None, site_ids=None, acknowledged_by=None):
    """Shared checkout logic used by both the admin page and student self-service.

    acknowledged_by MUST stay the last parameter — callers invoke this
    positionally, so inserting a new param earlier would silently shift
    site_ids into the wrong argument with no error."""
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag, is_loaner=True).first()
    if not row:
        return 'error', f'{asset_tag} is not a loaner device.'
    if site_ids is not None and row.site_id not in site_ids:
        return 'error', f'{asset_tag} is not one of your site\'s loaners.'
    already_out = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if already_out:
        return 'error', f'{asset_tag} is already checked out to {already_out.person_name}.'
    db.session.add(LoanerCheckout(
        asset_tag=asset_tag, person_id=person.id, person_name=person.full_name,
        due_date=due_date or (datetime.utcnow().date() + timedelta(days=LOANER_DEFAULT_LOAN_DAYS)),
        acknowledged_by=acknowledged_by,
    ))
    _log_activity('loaner_checkout', f'Checked out loaner {asset_tag} to {person.full_name}.', site_id=row.site_id)
    db.session.commit()
    _sync_loaner_google_state(row, enabled=True)
    message = f'Checked out {asset_tag} to {person.full_name}.'
    if row.site_id and person.site_id and row.site_id != person.site_id:
        message += ' Note: this loaner and person are at different sites.'
    return 'ok', message


def _checkin_loaner(asset_tag, condition_notes=None, site_ids=None):
    """Shared checkin logic used by both the admin page and student self-service."""
    open_row = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if not open_row:
        return 'error', f'{asset_tag} is not currently checked out as a loaner.'
    if site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            return 'error', f'{asset_tag} is not one of your site\'s loaners.'
    open_row.checked_in_at = datetime.utcnow()
    if condition_notes:
        open_row.condition_notes = condition_notes
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    _log_activity('loaner_checkin', f'Checked in loaner {asset_tag} (was with {open_row.person_name}).',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    if registry_row:
        _sync_loaner_google_state(registry_row, enabled=False)
    return 'ok', f'Checked in {asset_tag} (was with {open_row.person_name}).'


@app.route('/admin/loaners/checkout', methods=['POST'])
@require_permission('loaners')
def admin_loaners_checkout():
    site_ids = _current_site_ids()
    scan_value = request.form.get('asset_tag', '').strip()
    person_id = request.form.get('person_id', '').strip()
    due_date_str = request.form.get('due_date', '').strip()
    acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
    person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
    if not scan_value or not person:
        flash('Choose both a person and a loaner asset tag.', 'error')
        return redirect(url_for('admin_loaners'))
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        flash(f'"{scan_value}" was not found in the asset registry.', 'error')
        return redirect(url_for('admin_loaners'))
    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid due date.', 'error')
            return redirect(url_for('admin_loaners'))
    status, message = _checkout_loaner(asset_tag, person, due_date, site_ids, acknowledged_by=acknowledged_by)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(url_for('admin_loaners'))


@app.route('/admin/loaners/checkin', methods=['POST'])
@require_permission('loaners')
def admin_loaners_checkin():
    asset_tag = request.form.get('asset_tag', '').strip()
    status, message = _checkin_loaner(asset_tag, site_ids=_current_site_ids())
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(url_for('admin_loaners'))


@app.route('/loaner_checkout', methods=['GET', 'POST'])
@kiosk_or_permission_required('loaner_checkinout')
def loaner_checkout_page():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        scan_value = request.form.get('scan_value', '').strip()
        acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        if not acknowledged_by:
            flash('Type your name to acknowledge responsibility for this device.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            flash(f'"{scan_value}" was not found in the asset registry.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        status, message = _checkout_loaner(asset_tag, person, site_ids=site_ids, acknowledged_by=acknowledged_by)
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkout_page'))

    return render_template('loaner_checkout.html')


@app.route('/loaner_checkin', methods=['GET', 'POST'])
@kiosk_or_permission_required('loaner_checkinout')
def loaner_checkin_page():
    if request.method == 'POST':
        scan_value = request.form.get('scan_value', '').strip()
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkin_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value so a direct tag match on LoanerCheckout still works
        status, message = _checkin_loaner(asset_tag, site_ids=_current_site_ids())
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

    query = _scope_registry(AssetRegistry.query, _current_site_ids()).order_by(AssetRegistry.asset_tag)
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
    site_ids = _current_site_ids()
    if asset_tag and site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            asset_tag = None
    if not asset_tag:
        flash(f'No asset found matching "{value}".', 'error')
        return redirect(url_for('admin_audit', **redirect_args))

    db.session.add(AuditScan(asset_tag=asset_tag))
    db.session.commit()
    flash(f'Verified {asset_tag}.', 'success')
    return redirect(url_for('admin_audit', **redirect_args))


# ─── Incident / Damage Reports / Fees ─────────────────────────────────────────

def _person_unpaid_fee_total(person_id):
    """Sum of fee_amount across this person's charged-but-unpaid incidents.
    Used to warn (not block) on delete/graduate."""
    total = db.session.query(db.func.coalesce(db.func.sum(Incident.fee_amount), 0)).filter(
        Incident.person_id == person_id, Incident.fee_charged.is_(True), Incident.paid_at.is_(None),
    ).scalar()
    return Decimal(total)


def _create_incident(asset_tag, person, description, fee_charged=False, fee_amount=None):
    """Shared incident-logging logic used by both the admin page and the
    student self-service Report a Problem page. Does not commit — caller's
    responsibility, matching _assign_asset_to_person/_checkout_loaner."""
    incident = Incident(
        asset_tag=asset_tag, person_id=person.id if person else None,
        person_name=person.full_name if person else None,
        description=description, fee_charged=fee_charged, fee_amount=fee_amount,
    )
    db.session.add(incident)
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    _log_activity('incident_add', f'Logged incident on {asset_tag}: {description}',
                   site_id=registry_row.site_id if registry_row else None)
    return incident


@app.route('/admin/assets/<string:asset_tag>/incidents', methods=['POST'])
@require_permission('devices')
def admin_incident_add(asset_tag):
    """Logs a damage/loss report against an asset, snapshotting the currently
    assigned person. A fee_amount greater than zero forces fee_charged=True
    regardless of the checkbox — an amount implies a charge."""
    _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    description = request.form.get('description', '').strip()
    fee_charged = request.form.get('fee_charged') == 'on'
    fee_amount = _parse_money(request.form.get('fee_amount'))
    if fee_amount:
        fee_charged = True
    if not description:
        flash('Enter a description of the incident.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None
    try:
        _create_incident(asset_tag, person, description, fee_charged=fee_charged, fee_amount=fee_amount)
        db.session.commit()
        flash('Incident logged.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not log incident: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/report_problem', methods=['GET', 'POST'])
@kiosk_or_permission_required('checkinout')
def report_problem_page():
    """
    Student/staff self-service "something's wrong with my device" page — no
    fee fields exposed here, that's an office decision made later from the
    device's assign page. Mirrors loaner_checkout_page's shape: person-search
    + scan input, resolve_scan() with a raw-value fallback, site-check when scoped.
    """
    site_ids = _current_site_ids()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        scan_value = request.form.get('scan_value', '').strip()
        description = request.form.get('description', '').strip()
        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('report_problem_page'))
        if not scan_value:
            flash('Scan or type the device asset tag/serial.', 'error')
            return redirect(url_for('report_problem_page'))
        if not description:
            flash('Describe the problem.', 'error')
            return redirect(url_for('report_problem_page'))

        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value, same as loaner_checkin_page
        if site_ids is not None:
            row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
            if not row or row.site_id not in site_ids:
                flash(f'"{scan_value}" was not found in the asset registry.', 'error')
                return redirect(url_for('report_problem_page'))

        try:
            _create_incident(asset_tag, person, description)
            db.session.commit()
            flash('Thanks — your report has been logged.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Could not log report: {e}', 'error')
        return redirect(url_for('report_problem_page'))

    return render_template('report_problem.html')


@app.route('/admin/incidents/<int:incident_id>/mark_paid', methods=['POST'])
@require_permission('devices')
def admin_incident_mark_paid(incident_id):
    incident = Incident.query.get_or_404(incident_id)
    incident.paid_at = datetime.utcnow()
    registry_row = AssetRegistry.query.filter_by(asset_tag=incident.asset_tag).first()
    _log_activity('fee_paid', f'Marked fee paid for {incident.asset_tag} ({incident.person_name or "unknown"}).',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    flash('Marked paid.', 'success')
    return redirect(request.referrer or url_for('admin_asset_assign', asset_tag=incident.asset_tag))


@app.route('/admin/incidents/<int:incident_id>/fee', methods=['POST'])
@require_permission('devices')
def admin_incident_fee(incident_id):
    """Corrects an incident's fee amount after the fact (e.g. the office
    negotiated a lower repair cost than first estimated)."""
    incident = Incident.query.get_or_404(incident_id)
    fee_amount = _parse_money(request.form.get('fee_amount'))
    incident.fee_amount = fee_amount
    incident.fee_charged = bool(fee_amount)
    registry_row = AssetRegistry.query.filter_by(asset_tag=incident.asset_tag).first()
    _log_activity('fee_edit', f'Updated fee for {incident.asset_tag} to {fee_amount if fee_amount else "none"}.',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    flash('Updated fee.', 'success')
    return redirect(request.referrer or url_for('admin_asset_assign', asset_tag=incident.asset_tag))


@app.route('/admin/fees')
@require_permission('devices')
def admin_fees():
    """Who owes money — unpaid, charged incidents grouped by person."""
    site_ids = _current_site_ids()
    query = Incident.query.filter(Incident.fee_charged.is_(True), Incident.paid_at.is_(None))
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == Incident.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    unpaid = query.order_by(Incident.person_name, Incident.created_at).all()

    by_person = OrderedDict()
    grand_total = Decimal('0')
    for inc in unpaid:
        key = inc.person_name or '(no person on file)'
        by_person.setdefault(key, {'incidents': [], 'subtotal': Decimal('0')})
        amount = inc.fee_amount or Decimal('0')
        by_person[key]['incidents'].append(inc)
        by_person[key]['subtotal'] += amount
        grand_total += amount

    return render_template('admin_fees.html', by_person=by_person, grand_total=grand_total)


# ─── Repairs ──────────────────────────────────────────────────────────────────

def _scope_repairs(query, site_ids):
    """Repair has no site_id of its own — scope via a join through AssetRegistry,
    same pattern _overdue_assignments/_overdue_loaners use."""
    if site_ids is None:
        return query
    return query.join(AssetRegistry, AssetRegistry.asset_tag == Repair.asset_tag) \
        .filter(AssetRegistry.site_id.in_(site_ids))


@app.route('/admin/assets/<string:asset_tag>/repairs/send', methods=['POST'])
@require_permission('repairs')
def admin_repair_send(asset_tag):
    """Sends a device out for repair — creates the tracking record and sets
    the asset status to 'repair' together, in one transaction."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    if Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first():
        flash(f'{asset_tag} already has an open repair.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    vendor = request.form.get('vendor', '').strip() or None
    ticket_number = request.form.get('ticket_number', '').strip() or None
    issue_description = request.form.get('issue_description', '').strip() or None
    expected_return_at = _parse_date(request.form.get('expected_return_at'))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None
    try:
        db.session.add(Repair(
            asset_tag=asset_tag, vendor=vendor, ticket_number=ticket_number,
            issue_description=issue_description, expected_return_at=expected_return_at,
            person_name_snapshot=person.full_name if person else None,
        ))
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.status = 'repair'
        _log_activity('repair_send', f'Sent {asset_tag} to repair{" via " + vendor if vendor else ""}.', site_id=registry_row.site_id)
        db.session.commit()
        flash(f'{asset_tag} sent to repair.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not send device to repair: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/repairs/<int:repair_id>/return', methods=['POST'])
@require_permission('repairs')
def admin_repair_return(repair_id):
    """Marks an open repair returned. The outcome drives what happens to the
    device's status next: Fixed goes back into service, Could Not Repair and
    Replaced both retire this physical unit (a Replaced device's replacement
    is added separately as an ordinary new device, not automated here)."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    outcome = request.form.get('outcome', '')
    if outcome not in REPAIR_OUTCOMES:
        flash('Choose a valid outcome.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=repair.asset_tag))

    notes = request.form.get('notes', '').strip() or None
    asset = Asset.query.filter_by(asset_tag=repair.asset_tag).first()
    registry_row = AssetRegistry.query.filter_by(asset_tag=repair.asset_tag).first()
    try:
        repair.returned_at = datetime.utcnow()
        repair.outcome = outcome
        repair.notes = notes
        if asset:
            if outcome == 'fixed':
                asset.status = 'assigned' if asset.assigned_to_id else 'available'
            else:
                asset.status = 'retired'
        _log_activity('repair_return', f'{repair.asset_tag} returned from repair ({REPAIR_OUTCOMES[outcome]}).',
                       site_id=registry_row.site_id if registry_row else None)
        db.session.commit()
        flash(f'{repair.asset_tag} marked returned ({REPAIR_OUTCOMES[outcome]}).', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update repair: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=repair.asset_tag))


@app.route('/admin/repairs')
@require_permission('repairs')
def admin_repairs():
    """Fleet-wide open + recent-closed repair list."""
    site_ids = _current_site_ids()
    open_repairs = _scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.is_(None)) \
        .order_by(Repair.sent_at.desc()).all()
    closed_repairs = _scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.isnot(None)) \
        .order_by(Repair.returned_at.desc()).limit(50).all()
    return render_template('admin_repairs.html', open_repairs=open_repairs, closed_repairs=closed_repairs,
                           repair_outcomes=REPAIR_OUTCOMES, today=datetime.utcnow().date())


# ─── Activity Log ─────────────────────────────────────────────────────────────

ACTIVITY_LOG_ACTIONS = [
    'device_add', 'device_edit', 'device_delete', 'device_assign', 'device_unassign', 'device_status',
    'registry_csv_import', 'registry_set_sites',
    'person_add', 'person_edit', 'person_delete', 'person_reactivate', 'people_csv_import', 'people_graduate',
    'loaner_toggle', 'loaner_checkout', 'loaner_checkin', 'reminders_send',
    'incident_add', 'fee_paid', 'fee_edit',
    'repair_send', 'repair_return',
    'kiosk_enroll', 'kiosk_revoke',
    'user_add', 'user_edit', 'user_delete',
    'site_add', 'site_edit', 'site_delete',
]


def _scope_activity_log(query, site_ids):
    """A site-scoped admin only sees rows with a matching site_id; rows with
    no site (Users/Sites CRUD, a multi-site bulk import) are super-admin-only,
    since a None site_id can't be attributed to any one of their sites."""
    if site_ids is None:
        return query
    return query.filter(ActivityLog.site_id.in_(site_ids))


@app.route('/admin/activity')
@require_permission('admin')
def admin_activity():
    page = request.args.get('page', 1, type=int)
    actor_filter = request.args.get('actor', '').strip()
    action_filter = request.args.get('action', '').strip()
    since_str = request.args.get('since', '').strip()
    until_str = request.args.get('until', '').strip()

    query = _scope_activity_log(ActivityLog.query, _current_site_ids()).order_by(ActivityLog.timestamp.desc())
    if actor_filter:
        query = query.filter(ActivityLog.actor_label.ilike(f'%{actor_filter}%'))
    if action_filter in ACTIVITY_LOG_ACTIONS:
        query = query.filter(ActivityLog.action == action_filter)
    else:
        action_filter = ''
    since = _parse_date(since_str)
    if since:
        query = query.filter(ActivityLog.timestamp >= since)
    until = _parse_date(until_str)
    if until:
        query = query.filter(ActivityLog.timestamp < until + timedelta(days=1))

    pagination = query.paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_activity.html', pagination=pagination, actions=ACTIVITY_LOG_ACTIONS,
                           actor_filter=actor_filter, action_filter=action_filter,
                           since=since_str, until=until_str)


# ─── Scan / Check-in / Check-out API ─────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
@kiosk_or_api_permission_required('checkinout')
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

        if not unknown:
            site_ids = _current_site_ids()
            if site_ids is not None:
                row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
                if not row or row.site_id not in site_ids:
                    return jsonify({'error': f'"{scan_value}" was not found.'}), 404

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
        site_ids = _current_site_ids()
        query = Asset.query
        if site_ids is not None:
            query = query.join(AssetRegistry, AssetRegistry.asset_tag == Asset.asset_tag) \
                .filter(AssetRegistry.site_id.in_(site_ids))
        assets = query.order_by(Asset.asset_tag).all()
        return jsonify([a.to_dict() for a in assets])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/assets/<string:asset_tag>/history', methods=['GET'])
@api_login_required
def get_asset_history(asset_tag):
    site_ids = _current_site_ids()
    if site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            return jsonify({'message': f'No history found for {asset_tag}'}), 404
    events = Event.query.filter_by(asset_tag=asset_tag).order_by(Event.timestamp.desc()).all()
    if not events:
        return jsonify({'message': f'No history found for {asset_tag}'}), 404
    return jsonify([e.to_dict() for e in events])


@app.route('/healthz')
def healthz():
    """Unauthenticated liveness/readiness check for orchestrators and uptime
    monitoring — confirms the app can actually reach the database, not just
    that the process is running."""
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 503


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.route('/')
@kiosk_or_login_required
def index():
    return render_template('index.html')


@app.route('/checkin')
@kiosk_or_permission_required('checkinout')
def checkin_page():
    recent = Event.query.filter_by(action='checkin').order_by(Event.timestamp.desc()).limit(10).all()
    return render_template('scan.html', action='checkin', title='Check In', recent_events=recent)


@app.route('/checkout')
@kiosk_or_permission_required('checkinout')
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
    site_ids = _current_site_ids()
    asset_tag, _ = resolve_scan(query)

    if asset_tag and site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            asset_tag = None  # out of scope — treat as not found

    if not asset_tag:
        if site_ids is not None:
            # Unresolved/orphan tags have no site to attribute — super-admin-only, same as /admin/orphans
            return render_template('asset_history.html', query=query, resolved_tag=None, events=[])
        # If not in registry, fall back to searching events directly
        asset_tag = query

    events = Event.query.filter_by(asset_tag=asset_tag).order_by(Event.timestamp.desc()).all()
    return render_template('asset_history.html',
                           query=query,
                           resolved_tag=asset_tag if asset_tag != query else None,
                           events=events)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=DEBUG_MODE)