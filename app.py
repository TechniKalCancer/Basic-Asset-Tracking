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


# ─── Models ───────────────────────────────────────────────────────────────────

class Site(db.Model):
    """A school/building. The unit that people, devices, and admin users are scoped to."""
    __tablename__ = 'site'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), unique=True, nullable=False, index=True)
    google_loaner_autodisable_enabled = db.Column(db.Boolean, nullable=False, default=False)  # per-site opt-in pilot gate, see _sync_device_google_state — despite the name, also gates the OU-move-to-borrower behavior on assignment
    loaner_org_unit_path = db.Column(db.String(255), nullable=True)  # where this site's loaner Chromebooks should live in Google Workspace — see _push_loaners_to_ou
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
    logo_background      = db.Column(db.String(10), nullable=True)  # None (transparent) | 'light' | 'dark' — a plate behind the logo so a dark/light-only logo stays readable regardless of what's uploaded
    favicon_filename     = db.Column(db.String(255), nullable=True)
    primary_color_raw    = db.Column(db.String(7), nullable=True)
    primary_color        = db.Column(db.String(7), nullable=True)  # = --accent (contrast-nudged for readability on the dark bg)
    accent_dim_color     = db.Column(db.String(7), nullable=True)
    accent_text_color    = db.Column(db.String(7), nullable=True)
    secondary_color      = db.Column(db.String(7), nullable=True)
    secondary_text_color = db.Column(db.String(7), nullable=True)
    tertiary_color       = db.Column(db.String(7), nullable=True)
    tertiary_text_color  = db.Column(db.String(7), nullable=True)
    updated_at           = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmailSettings(db.Model):
    """
    Single-row (id=1) customizable wording for every system email this app
    sends, editable at /admin/emails. Each subject/body column is nullable —
    null means "use the built-in default" (EMAIL_TEMPLATE_KINDS below), so
    upgrading never breaks an install and an admin only has to touch the
    ones they actually want to change. Templates use plain {variable}
    placeholders substituted at send time by _render_email_template().
    """
    __tablename__ = 'email_settings'
    id                          = db.Column(db.Integer, primary_key=True)
    loaner_overdue_subject      = db.Column(db.String(200), nullable=True)
    loaner_overdue_body         = db.Column(db.Text, nullable=True)
    loaner_upcoming_subject     = db.Column(db.String(200), nullable=True)
    loaner_upcoming_body        = db.Column(db.Text, nullable=True)
    loaner_nodate_subject       = db.Column(db.String(200), nullable=True)
    loaner_nodate_body          = db.Column(db.Text, nullable=True)
    assignment_overdue_subject  = db.Column(db.String(200), nullable=True)
    assignment_overdue_body     = db.Column(db.Text, nullable=True)
    updated_at                  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomField(db.Model):
    """
    A user-defined extra field on Person or AssetRegistry — lets an admin
    capture data this app doesn't have a real column for (without a code
    change/migration each time) via /admin/custom_fields. Values live in
    that row's own custom_fields JSON column, keyed by field_key; this
    table is just the field's definition (what exists, what it's called).
    """
    __tablename__ = 'custom_field'
    id          = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(20), nullable=False)  # 'person' | 'device'
    field_key   = db.Column(db.String(60), nullable=False)  # slug, used as the JSON key
    label       = db.Column(db.String(120), nullable=False)
    field_type  = db.Column(db.String(20), nullable=False, default='text')  # 'text' | 'number' | 'date' | 'boolean' | 'email'
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('entity_type', 'field_key', name='uq_custom_field_entity_key'),)


class GoogleFieldMapping(db.Model):
    """
    Maps one field from a Google Directory API record onto one target field
    on Person or AssetRegistry, applied by _run_google_sync() at
    /admin/google_field_mapping. google_field is a dotted path into the raw
    API response (e.g. 'name.givenName', 'orgUnitPath', 'phones.0.value') —
    see _get_nested_value(). target_field is either a real column name (from
    PERSON_SYNC_TARGET_FIELDS/DEVICE_SYNC_TARGET_FIELDS) or 'custom:<key>'
    referencing a CustomField.
    """
    __tablename__ = 'google_field_mapping'
    id           = db.Column(db.Integer, primary_key=True)
    entity_type  = db.Column(db.String(20), nullable=False)  # 'person' | 'device'
    google_field = db.Column(db.String(120), nullable=False)
    target_field = db.Column(db.String(80), nullable=False)
    org_unit_scope = db.Column(db.String(255), nullable=True)  # None=all; '__staff__'/'__student__'=category; else an exact org unit path — see _mapping_applies_to_org_unit()
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class GoogleOrgUnit(db.Model):
    """
    A cached copy of one Google Workspace Organizational Unit, refreshed from
    the Admin SDK at /admin/google_org_units. category is set by hand (not by
    Google) so an admin can bucket each OU as staff or student — that bucket
    is then usable as a GoogleFieldMapping.org_unit_scope, and/or as the
    source for a mapping that writes 'staff'/'student' onto Person.role.
    site_id is a second, independent hand-set tag — which building (Site)
    this org unit belongs to, so _run_google_people_sync/_run_google_device_sync
    can correct a Person's/AssetRegistry's site_id straight from Google's own
    org unit, without needing IP/network guesswork. Kept as its own table
    (not folded into the mapping form) since one org unit tree is shared by
    every mapping, for both entity types.
    """
    __tablename__ = 'google_org_unit'
    id            = db.Column(db.Integer, primary_key=True)
    org_unit_path = db.Column(db.String(255), unique=True, nullable=False)
    name          = db.Column(db.String(255), nullable=True)
    category      = db.Column(db.String(20), nullable=False, default='unclassified')  # 'unclassified' | 'staff' | 'student'
    site_id       = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)
    updated_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    site = db.relationship('Site')



class UserSite(db.Model):
    """Join table: which sites a (non-super-admin) User account can see/manage."""
    __tablename__ = 'user_site'
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint('user_id', 'site_id', name='uq_user_site'),)


class DeviceModel(db.Model):
    """
    A catalog entry for a specific make/model (e.g. "Dell Chromebook 3120"),
    distinct from the coarse device_type (chromebook/laptop/ipad/...) every
    AssetRegistry row already carries. Picking a model on Add/Edit Device is
    optional and just suggests a device_type client-side — device_type stays
    the required, independently-editable field driving every existing
    filter/icon, so nothing already built against it breaks.
    """
    __tablename__ = 'device_model'
    id           = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(80), nullable=False)
    model_name   = db.Column(db.String(120), nullable=False)
    device_type  = db.Column(db.String(40), nullable=False, default='chromebook')
    notes        = db.Column(db.Text, nullable=True)
    is_active    = db.Column(db.Boolean, nullable=False, default=True)
    __table_args__ = (db.UniqueConstraint('manufacturer', 'model_name', name='uq_device_model_make_model'),)

    @property
    def full_name(self):
        return f'{self.manufacturer} {self.model_name}'


class AssetNumberRange(db.Model):
    """
    A block of asset-tag numbers reserved for prebaked/pre-printed labels
    that don't exist in the registry yet. _generate_asset_tag() skips any
    candidate falling inside a range, so the random-tag generator never
    hands out a number the physical labels already claim. Deliberately
    doesn't constrain manual tag entry — the whole point is letting someone
    key in a number from a prebaked range themselves.

    At most one range has is_default=True at a time (enforced in the routes,
    not the DB) — when set, _generate_asset_tag() pulls the next sequential
    number from that range instead of picking randomly from the whole space,
    for BOTH the single Add Device flow and CSV bulk import, so an entire
    batch of pre-printed labels gets handed out in order without anyone
    having to pick the range by hand every time.
    """
    __tablename__ = 'asset_number_range'
    id          = db.Column(db.Integer, primary_key=True)
    label       = db.Column(db.String(120), nullable=False)
    range_start = db.Column(db.Integer, nullable=False)
    range_end   = db.Column(db.Integer, nullable=False)
    is_default  = db.Column(db.Boolean, nullable=False, default=False)
    notes       = db.Column(db.Text, nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


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
    device_model_id = db.Column(db.Integer, db.ForeignKey('device_model.id'), nullable=True, index=True)
    is_loaner     = db.Column(db.Boolean, nullable=False, default=False, index=True)  # part of the short-term loaner pool, not permanently assigned to anyone
    site_id       = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True, index=True)
    purchase_date = db.Column(db.Date, nullable=True)
    purchase_cost = db.Column(db.Numeric(10, 2), nullable=True)
    warranty_expiration = db.Column(db.Date, nullable=True)
    custom_fields = db.Column(db.JSON, nullable=True)  # {field_key: value, ...} — see CustomField/GoogleFieldMapping

    site = db.relationship('Site')
    device_model = db.relationship('DeviceModel')

    def to_dict(self):
        return {
            'asset_tag': self.asset_tag,
            'serial_number': self.serial_number,
            'description': self.description,
            'device_type': self.device_type,
            'device_model': self.device_model.full_name if self.device_model else None,
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
    custom_fields = db.Column(db.JSON, nullable=True)  # {field_key: value, ...} — see CustomField/GoogleFieldMapping

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
    can_tickets   = db.Column(db.Boolean, nullable=False, default=False)  # help-desk ticket queue: view/comment/assign/resolve, manage categories
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


class RepairCategory(db.Model):
    """
    A standard damage/repair type (e.g. "Cracked Screen", "Broken Hinge",
    "Lost Charger") with its usual price — same shape as TicketCategory,
    kept as a separate catalog since it describes device damage specifically
    rather than general help-desk issues. Picking one on Log Incident
    auto-fills the fee amount; multiple incidents on the same asset (each
    optionally tagged with a category) are what "list of damages" on the
    printable invoice comes from — no separate line-item table needed.
    """
    __tablename__ = 'repair_category'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(80), unique=True, nullable=False)
    default_price = db.Column(db.Numeric(8, 2), nullable=True)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)


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
    repair_category_id = db.Column(db.Integer, db.ForeignKey('repair_category.id'), nullable=True)
    description = db.Column(db.Text, nullable=False)
    fee_charged = db.Column(db.Boolean, nullable=False, default=False)
    fee_amount  = db.Column(db.Numeric(8, 2), nullable=True)
    paid_at     = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    repair_category = db.relationship('RepairCategory')


class Repair(db.Model):
    """
    A device sent out for RMA/repair — separate from the plain 'repair' Asset
    status label, this is the actual tracking record (category, ticket, dates).
    Wired to the can('repairs') permission. person_name_snapshot mirrors the
    same pattern as Incident/AssignmentHistory: readable even if the person
    who had the device is later deleted. ticket_id links to a Ticket opened
    automatically alongside the repair (_send_device_to_repair) so it shows
    up in the normal help-desk queue instead of living outside it — distinct
    from ticket_number, which is the vendor's own free-text RMA reference.
    repair_category_id reuses the same RepairCategory catalog Incident uses
    (optional, same reasoning: classify the repair without forcing a choice
    when nothing fits).
    """
    __tablename__ = 'repair'
    id                   = db.Column(db.Integer, primary_key=True)
    asset_tag            = db.Column(db.String(120), nullable=False, index=True)
    repair_category_id   = db.Column(db.Integer, db.ForeignKey('repair_category.id'), nullable=True)
    ticket_number        = db.Column(db.String(80), nullable=True)
    issue_description    = db.Column(db.Text, nullable=True)  # required at the form level (see admin_repair_send*/admin_repair_edit) — nullable here so existing rows with none don't need a migration backfill, same reasoning as the registry serial-number requirement
    person_name_snapshot = db.Column(db.String(160), nullable=True)
    sent_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expected_return_at   = db.Column(db.Date, nullable=True)
    returned_at          = db.Column(db.DateTime, nullable=True)
    outcome              = db.Column(db.String(20), nullable=True)  # 'fixed' | 'could_not_repair' | 'replaced'
    notes                = db.Column(db.Text, nullable=True)
    ticket_id            = db.Column(db.Integer, db.ForeignKey('ticket.id'), nullable=True)

    repair_category = db.relationship('RepairCategory')
    ticket = db.relationship('Ticket', backref=db.backref('repair', uselist=False))


REPAIR_OUTCOMES = {
    'fixed': 'Fixed',
    'could_not_repair': 'Could Not Repair',
    'replaced': 'Replaced',
}


class TicketCategory(db.Model):
    """A help-desk ticket category (e.g. "Network", "Software", "Hardware").
    Flat list, no subcategories — kept simple until there's an actual need
    for nesting. is_active=False retires a category without breaking
    existing tickets still referencing it. default_price is the standard
    charge for this kind of issue (e.g. "Lost Charger" = $15) — applied to a
    new ticket automatically at creation time, same fee_charged/fee_amount/
    paid_at shape Incident already uses for damage-report billing."""
    __tablename__ = 'ticket_category'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(80), unique=True, nullable=False)
    default_price = db.Column(db.Numeric(8, 2), nullable=True)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)


class Ticket(db.Model):
    """
    A general IT help-desk request — unlike Incident (damage/fee reports)
    or Repair (RMA tracking), a ticket doesn't have to be tied to a specific
    asset ("wifi is down in room 204"). requester_name/requester_email are a
    snapshot, same reasoning as Incident.person_name: stays readable if the
    Person record is later deleted or the ticket was filed for someone
    without a Person record at all (a parent, a walk-up visitor). Billing
    lives entirely in TicketCharge (below) — a ticket can accumulate several
    distinct charges over its life, so there's no single fee_amount here.
    """
    __tablename__ = 'ticket'
    id            = db.Column(db.Integer, primary_key=True)
    category_id   = db.Column(db.Integer, db.ForeignKey('ticket_category.id'), nullable=False)
    subject       = db.Column(db.String(200), nullable=False)
    description   = db.Column(db.Text, nullable=False)
    status        = db.Column(db.String(20), nullable=False, default='open', index=True)
    priority      = db.Column(db.String(20), nullable=False, default='normal')
    site_id       = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True, index=True)
    asset_tag     = db.Column(db.String(120), nullable=True, index=True)
    requester_person_id = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True)
    requester_name  = db.Column(db.String(160), nullable=True)
    requester_email = db.Column(db.String(160), nullable=True)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at   = db.Column(db.DateTime, nullable=True)

    category     = db.relationship('TicketCategory')
    site         = db.relationship('Site')
    requester    = db.relationship('Person')
    assigned_to  = db.relationship('User')
    comments     = db.relationship('TicketComment', backref='ticket', order_by='TicketComment.created_at',
                                    cascade='all, delete-orphan')
    charges      = db.relationship('TicketCharge', backref='ticket', order_by='TicketCharge.created_at',
                                    cascade='all, delete-orphan')


class TicketComment(db.Model):
    """An internal note on a Ticket. There's no submitter-facing ticket
    portal/login in this app (public forms are anonymous + person-search
    based, not accounts), so every comment is inherently admin-internal —
    no is_internal flag, it would have no meaningful False case."""
    __tablename__ = 'ticket_comment'
    id          = db.Column(db.Integer, primary_key=True)
    ticket_id   = db.Column(db.Integer, db.ForeignKey('ticket.id'), nullable=False, index=True)
    body        = db.Column(db.Text, nullable=False)
    author_label = db.Column(db.String(160), nullable=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TicketCharge(db.Model):
    """One itemized billing line on a Ticket — a ticket can rack up more than
    one distinct charge over its life (e.g. a lost charger AND a cracked
    case reported on the same help-desk request), same reasoning as why
    Incident supports multiple rows per asset instead of a single fee field."""
    __tablename__ = 'ticket_charge'
    id          = db.Column(db.Integer, primary_key=True)
    ticket_id   = db.Column(db.Integer, db.ForeignKey('ticket.id'), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    amount      = db.Column(db.Numeric(8, 2), nullable=False)
    paid_at     = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


TICKET_STATUSES = ['open', 'in_progress', 'resolved', 'closed']
TICKET_PRIORITIES = ['low', 'normal', 'high', 'urgent']


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
    ticket_id     = db.Column(db.Integer, db.ForeignKey('ticket.id'), nullable=True, index=True)  # set only for ticket_* actions, powers the per-ticket History panel
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
    repair_id        = db.Column(db.Integer, db.ForeignKey('repair.id'), nullable=True, index=True)  # set when this loaner covers someone whose own device is out for repair — see _checkout_loaner/admin_repair_assign_loaner

    repair = db.relationship('Repair', backref='loaner_checkouts')


# Schema creation/upgrades are handled by Flask-Migrate (`flask db upgrade`),
# run once from entrypoint.sh before gunicorn starts — not here. Running it
# in-process per gunicorn worker (the old db.create_all() approach) doesn't
# work safely with Alembic's single version-tracking table the way it did
# with create_all()'s idempotent CREATE TABLE IF NOT EXISTS-like behavior.
# For local dev outside Docker, run `flask db upgrade` once yourself first.


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _generate_asset_tag(existing_tags):
    """
    Self-assigns a tag when one isn't provided, whether adding a device
    manually or importing a CSV row with no asset_tag/serial.

    If a default AssetNumberRange is set, pulls the next sequential number
    from it (same as picking that range by hand on Add Device) — this is
    what makes a CSV import of a whole batch of pre-printed labels "just
    work" without anyone selecting a range per row. Falls through to the
    old whole-space random behavior (still avoiding every reserved range,
    default or not) if there's no default range or it's fully used, so a
    bulk import never hard-fails partway through just because one batch
    ran out.
    """
    default_range = AssetNumberRange.query.filter_by(is_default=True).first()
    if default_range:
        tag = _next_tag_in_range(existing_tags, default_range.range_start, default_range.range_end)
        if tag:
            return tag

    reserved = [(r.range_start, r.range_end) for r in AssetNumberRange.query.all()]
    for _ in range(50):
        candidate = secrets.randbelow(900000) + 100000
        if str(candidate) in existing_tags:
            continue
        if any(start <= candidate <= end for start, end in reserved):
            continue
        return str(candidate)
    raise RuntimeError('Could not generate a unique asset tag after 50 attempts.')


def _next_tag_in_range(existing_tags, range_start, range_end):
    """
    Returns the lowest unused number in [range_start, range_end] as a string,
    or None if every number in the range is already taken. Used when an admin
    deliberately picks a reserved AssetNumberRange to draw from on Add Device
    — unlike _generate_asset_tag's random pick avoiding every reserved range,
    this pulls sequentially from inside ONE chosen range, matching a physical
    batch of pre-printed labels (grab the next sticker in the stack).
    """
    for candidate in range(range_start, range_end + 1):
        if str(candidate) not in existing_tags:
            return str(candidate)
    return None


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
        raise ValueError(f'Unsupported file type "{ext}". Use PNG, JPG, SVG, WebP, or ICO.')
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


# ─── Configurable Google field sync (People + Devices) ─────────────────────────
# A generalized version of sync_chromeos_device_from_google() above (which
# only ever pulls 3 fixed fields for one device at a time): an admin defines
# CustomField columns and GoogleFieldMapping rows at /admin/google_field_mapping,
# then _run_google_people_sync()/_run_google_device_sync() pull every user or
# ChromeOS device from Workspace, match by email/serial number, and apply
# whatever mappings exist onto the matched Person/AssetRegistry row. Deliberately
# match-only — never creates a new Person/AssetRegistry row from Google data
# alone, since the SIS roster (not Google's pre-provisioned accounts) is this
# district's source of truth for who's actually enrolled.

PERSON_SYNC_TARGET_FIELDS = {
    'first_name': 'First Name', 'last_name': 'Last Name',
    'role': 'Role', 'department': 'Department',
}
DEVICE_SYNC_TARGET_FIELDS = {
    'description': 'Description', 'device_type': 'Device Type',
}


def _get_nested_value(data, dotted_path):
    """Resolves a dotted path like 'name.givenName' or 'phones.0.value'
    against a nested dict/list (as returned by the Google API). Returns
    None if any segment along the way is missing, rather than raising —
    a mapping referencing a field a given record just doesn't have is a
    normal, expected case (e.g. not everyone has a phones entry)."""
    current = data
    for part in dotted_path.split('.'):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


ORG_UNIT_SCOPE_STAFF   = '__staff__'
ORG_UNIT_SCOPE_STUDENT = '__student__'
ORG_UNIT_SCOPE_CHOICES = {ORG_UNIT_SCOPE_STAFF: 'Staff (by org unit)', ORG_UNIT_SCOPE_STUDENT: 'Student (by org unit)'}


def _classify_org_unit(org_unit_path):
    """Returns 'staff'/'student' for the given org unit path, based on the
    closest classified ancestor in GoogleOrgUnit (so a sub-OU like
    '/Students/Class of 2030/Section A' inherits its parent's classification
    even if only '/Students' was tagged). Returns None if nothing matches."""
    if not org_unit_path:
        return None
    best_category, best_len = None, -1
    for ou in GoogleOrgUnit.query.filter(GoogleOrgUnit.category.in_(['staff', 'student'])).all():
        path = ou.org_unit_path
        if org_unit_path == path or org_unit_path.startswith(path.rstrip('/') + '/'):
            if len(path) > best_len:
                best_category, best_len = ou.category, len(path)
    return best_category


def _org_unit_site_id(org_unit_path):
    """Returns the Site id tagged on the closest classified ancestor of
    org_unit_path in GoogleOrgUnit (same closest-ancestor logic as
    _classify_org_unit, but for the hand-set site_id instead of category).
    Returns None if no ancestor has a Site tagged."""
    if not org_unit_path:
        return None
    best_site_id, best_len = None, -1
    for ou in GoogleOrgUnit.query.filter(GoogleOrgUnit.site_id.isnot(None)).all():
        path = ou.org_unit_path
        if org_unit_path == path or org_unit_path.startswith(path.rstrip('/') + '/'):
            if len(path) > best_len:
                best_site_id, best_len = ou.site_id, len(path)
    return best_site_id


def _mapping_applies_to_org_unit(mapping, org_unit_path):
    """Whether mapping's org_unit_scope allows it to apply to a record in
    org_unit_path — unscoped (None) mappings always apply; '__staff__'/
    '__student__' apply based on _classify_org_unit(); anything else is
    treated as an exact org unit path (also matching its sub-OUs)."""
    scope = mapping.org_unit_scope
    if not scope:
        return True
    if scope == ORG_UNIT_SCOPE_STAFF:
        return _classify_org_unit(org_unit_path) == 'staff'
    if scope == ORG_UNIT_SCOPE_STUDENT:
        return _classify_org_unit(org_unit_path) == 'student'
    return bool(org_unit_path) and (org_unit_path == scope or org_unit_path.startswith(scope.rstrip('/') + '/'))


def _apply_field_mappings(google_record, obj, mappings):
    """Applies every mapping onto obj (a Person or AssetRegistry instance)
    from the raw Google record. Mappings scoped to a specific org unit or
    org-unit category (see _mapping_applies_to_org_unit) are skipped for
    records outside that scope. Real-column targets are set directly;
    'custom:<key>' targets go into obj.custom_fields. Returns True if
    anything actually changed (so the caller can count real updates, not
    just matches). Does not commit."""
    changed = False
    custom = dict(obj.custom_fields or {})
    org_unit_path = google_record.get('orgUnitPath')
    for m in mappings:
        if not _mapping_applies_to_org_unit(m, org_unit_path):
            continue
        value = _get_nested_value(google_record, m.google_field)
        if value is None:
            continue
        value = str(value)
        if m.target_field.startswith('custom:'):
            key = m.target_field.split(':', 1)[1]
            if custom.get(key) != value:
                custom[key] = value
                changed = True
        elif hasattr(obj, m.target_field) and getattr(obj, m.target_field) != value:
            setattr(obj, m.target_field, value)
            changed = True
    if changed:
        obj.custom_fields = custom
    return changed


def _fetch_google_org_units():
    """Pulls the full Organizational Unit tree from the Admin SDK (shared by
    both Users and ChromeOS devices) and upserts it into GoogleOrgUnit —
    inserting new paths, refreshing the display name on existing ones, and
    leaving each row's hand-set category untouched. Returns the number of
    org units seen."""
    service = _google_directory_service([GOOGLE_SCOPE_ORGUNIT_READONLY])
    response = service.orgunits().list(customerId='my_customer', type='all').execute()
    org_units = response.get('organizationUnits', [])
    existing = {ou.org_unit_path: ou for ou in GoogleOrgUnit.query.all()}
    for entry in org_units:
        path = entry.get('orgUnitPath')
        if not path:
            continue
        name = entry.get('name')
        if path in existing:
            existing[path].name = name
        else:
            db.session.add(GoogleOrgUnit(org_unit_path=path, name=name))
    db.session.commit()
    return len(org_units)


# ─── Pushing FoxDesk's own data back onto Google org units ─────────────────
# The inverse of the pull-based site correction above: here FoxDesk's Site
# (for loaner Chromebooks) or grad_year (for students) decides where a
# device/account belongs, and we move it in Google to match. Devices move in
# one batch call each (moveDevicesToOu); Users have no equivalent bulk
# endpoint, so each account is patched individually.

def _push_loaners_to_ou(site):
    """Moves every loaner-pool device at `site` with a serial number into
    site.loaner_org_unit_path in Google Workspace. Returns (moved, not_found)
    — not_found counts loaner rows whose serial has no matching Chrome
    device in Google (e.g. it's actually a charger, not a Chromebook).
    Raises on auth/API failure so the caller can flash the real error."""
    if not site.loaner_org_unit_path:
        return 0, 0
    rows = AssetRegistry.query.filter_by(is_loaner=True, site_id=site.id) \
        .filter(AssetRegistry.serial_number.isnot(None)).all()
    if not rows:
        return 0, 0
    target_serials = {r.serial_number for r in rows}
    service = _google_directory_service([GOOGLE_SCOPE_MANAGE])
    found = {}
    page_token = None
    while True:
        response = service.chromeosdevices().list(
            customerId='my_customer', maxResults=200, pageToken=page_token, projection='BASIC',
        ).execute()
        for d in response.get('chromeosdevices', []):
            sn = d.get('serialNumber')
            if sn in target_serials:
                found[sn] = d['deviceId']
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    not_found = len(target_serials) - len(found)
    device_ids = list(found.values())
    for i in range(0, len(device_ids), 50):  # moveDevicesToOu caps out well under this per call
        service.chromeosdevices().moveDevicesToOu(
            customerId='my_customer', orgUnitPath=site.loaner_org_unit_path,
            body={'deviceIds': device_ids[i:i + 50]},
        ).execute()
    return len(device_ids), not_found


def _run_google_people_sync():
    """Pulls every Google Workspace user, matches to an existing Person by
    email, and applies each entity_type='person' GoogleFieldMapping onto the
    match — plus, independently of any mapping, corrects site_id from the
    account's org unit if that org unit (or an ancestor) has a Site tagged
    at /admin/google_org_units (see _org_unit_site_id). Returns (matched,
    updated, unmatched_google_accounts)."""
    mappings = GoogleFieldMapping.query.filter_by(entity_type='person').all()
    has_site_rules = GoogleOrgUnit.query.filter(GoogleOrgUnit.site_id.isnot(None)).first() is not None
    if not mappings and not has_site_rules:
        return 0, 0, 0
    service = _google_directory_service([GOOGLE_SCOPE_USER_READONLY])
    matched = updated = unmatched = 0
    page_token = None
    while True:
        response = service.users().list(
            customer='my_customer', maxResults=200, pageToken=page_token,
        ).execute()
        for u in response.get('users', []):
            email = u.get('primaryEmail')
            person = Person.query.filter_by(email=email).first() if email else None
            if not person:
                unmatched += 1
                continue
            matched += 1
            row_changed = _apply_field_mappings(u, person, mappings)
            site_id = _org_unit_site_id(u.get('orgUnitPath'))
            if site_id and person.site_id != site_id:
                person.site_id = site_id
                row_changed = True
            if row_changed:
                updated += 1
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    db.session.commit()
    return matched, updated, unmatched


def _run_google_device_sync():
    """Pulls every Google Workspace ChromeOS device, matches to an existing
    AssetRegistry row by serial number, and applies each entity_type='device'
    GoogleFieldMapping onto the match — plus, independently of any mapping,
    corrects site_id from the device's org unit the same way
    _run_google_people_sync does for People. Returns (matched, updated,
    unmatched)."""
    mappings = GoogleFieldMapping.query.filter_by(entity_type='device').all()
    has_site_rules = GoogleOrgUnit.query.filter(GoogleOrgUnit.site_id.isnot(None)).first() is not None
    if not mappings and not has_site_rules:
        return 0, 0, 0
    service = _google_directory_service([GOOGLE_SCOPE_READONLY])
    matched = updated = unmatched = 0
    page_token = None
    while True:
        response = service.chromeosdevices().list(
            customerId='my_customer', maxResults=200, pageToken=page_token,
        ).execute()
        for d in response.get('chromeosdevices', []):
            serial = d.get('serialNumber')
            row = AssetRegistry.query.filter_by(serial_number=serial).first() if serial else None
            if not row:
                unmatched += 1
                continue
            matched += 1
            row_changed = _apply_field_mappings(d, row, mappings)
            site_id = _org_unit_site_id(d.get('orgUnitPath'))
            if site_id and row.site_id != site_id:
                row.site_id = site_id
                row_changed = True
            if row_changed:
                updated += 1
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    db.session.commit()
    return matched, updated, unmatched


def _move_device_to_persons_ou(registry_row, person):
    """Looks up person's current Google org unit and moves registry_row's
    Chrome device there, so a device someone is now holding picks up their
    Chrome policies instead of whatever OU it was sitting in before (e.g. a
    loaner pool OU). No-ops if the person has no Google account, or the
    device has no matching Chrome device in Google. Raises on other API
    failures — caller (_sync_device_google_state) catches and logs."""
    service = _google_directory_service([GOOGLE_SCOPE_USER_READONLY, GOOGLE_SCOPE_MANAGE])
    from googleapiclient.errors import HttpError
    try:
        user = service.users().get(userKey=person.email, projection='basic').execute()
    except HttpError as e:
        if e.resp.status == 404:
            return
        raise
    org_unit_path = user.get('orgUnitPath')
    if not org_unit_path:
        return
    device = _find_chromeos_device_by_serial(service, registry_row.serial_number)
    if not device:
        return
    service.chromeosdevices().moveDevicesToOu(
        customerId='my_customer', orgUnitPath=org_unit_path, body={'deviceIds': [device['deviceId']]},
    ).execute()


def _sync_device_google_state(registry_row, enabled, person=None):
    """
    Best-effort Google enable/disable for a device on checkout/checkin or
    assignment — and, when enabling to a specific person, also moves the
    device into whichever org unit that person's own Google account
    currently lives in, so a loaner/assigned Chromebook picks up the
    borrower's Chrome policies instead of sitting in a pool/storage OU.

    No-ops (logs and returns) unless GOOGLE_SYNC_ENABLED, the separate
    GOOGLE_LOANER_AUTO_DISABLE_ENABLED env var, AND this device's site's opt-in
    flag are all true, and the device has a serial number on file. Never raises
    and never touches db.session for the enable/disable half — the
    checkout/checkin/assignment has already committed by the time this
    runs, so a Google-side failure shouldn't roll back the local action or
    block the person waiting on it. The OU-move half is separately
    best-effort for the same reason.
    """
    if not (GOOGLE_SYNC_ENABLED and GOOGLE_LOANER_AUTO_DISABLE_ENABLED):
        return
    if not (registry_row.site and registry_row.site.google_loaner_autodisable_enabled):
        return
    if not registry_row.serial_number:
        logger.info('Skipping Google auto-%s for %s: no serial number on file.',
                    'enable' if enabled else 'disable', registry_row.asset_tag)
        return
    try:
        set_chromeos_device_enabled(registry_row.serial_number, enabled)
        asset = Asset.query.filter_by(asset_tag=registry_row.asset_tag).first()
        if asset:
            asset.google_enabled = enabled
            db.session.commit()
    except Exception as e:
        logger.error('Google auto-%s failed for %s: %s',
                     'enable' if enabled else 'disable', registry_row.asset_tag, e)

    if enabled and person and person.email:
        try:
            _move_device_to_persons_ou(registry_row, person)
        except Exception as e:
            logger.error('Failed to move %s into %s\'s org unit: %s', registry_row.asset_tag, person.email, e)


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


# ─── Customizable email wording ────────────────────────────────────────────────
# Every system email this app sends is registered here as a "kind" with a
# built-in default subject/body. An admin can override either at
# /admin/emails (stored on EmailSettings); _render_email_template() applies
# the override if set, otherwise falls back to the default below. Keeping
# the defaults here (not hardcoded inline at each send_email() call site)
# means the customization UI and the actual send path can never drift apart.
EMAIL_TEMPLATE_KINDS = {
    'loaner_overdue': {
        'label': 'Loaner Reminder — Overdue',
        'subject': 'Reminder: loaner {asset_tag} is overdue for return',
        'body': (
            'Hi {first_name},\n\n'
            'Our records show loaner device {asset_tag} was due back on '
            '{due_date} ({days_overdue} day{days_overdue_plural} ago).\n\n'
            'Please return it to the office as soon as possible. If you\'ve already returned it, '
            'this reminder can be ignored.\n\nThanks!'
        ),
    },
    'loaner_upcoming': {
        'label': 'Loaner Reminder — Due Soon',
        'subject': 'Reminder: loaner {asset_tag} is due back {due_date}',
        'body': (
            'Hi {first_name},\n\n'
            'Just a reminder that loaner device {asset_tag} is due back on {due_date}.\n\n'
            'Please return it to the office by then. If you\'ve already returned it, '
            'this reminder can be ignored.\n\nThanks!'
        ),
    },
    'loaner_nodate': {
        'label': 'Loaner Reminder — No Due Date Set',
        'subject': 'Reminder: please return loaner {asset_tag}',
        'body': (
            'Hi {first_name},\n\n'
            'Just a reminder that you currently have loaner device {asset_tag} checked out. '
            'Please return it to the office when you\'re done with it.\n\n'
            'If you\'ve already returned it, this reminder can be ignored.\n\nThanks!'
        ),
    },
    'assignment_overdue': {
        'label': 'Assigned Device Reminder — Overdue',
        'subject': 'Reminder: {asset_tag} is overdue for return',
        'body': (
            'Hi {first_name},\n\n'
            'Our records show asset {asset_tag} was due back on '
            '{due_date} ({days_overdue} day{days_overdue_plural} ago).\n\n'
            'Please return it as soon as possible. If you\'ve already returned it, this reminder can be ignored.\n\n'
            'Thanks!'
        ),
    },
}

EMAIL_TEMPLATE_VARIABLES = {
    'loaner_overdue': ['first_name', 'full_name', 'asset_tag', 'due_date', 'days_overdue'],
    'loaner_upcoming': ['first_name', 'full_name', 'asset_tag', 'due_date'],
    'loaner_nodate': ['first_name', 'full_name', 'asset_tag'],
    'assignment_overdue': ['first_name', 'full_name', 'asset_tag', 'due_date', 'days_overdue'],
}


class _SafeFormatDict(dict):
    """Used with str.format_map() so a template referencing an unknown or
    misspelled variable name renders the literal {placeholder} instead of
    raising KeyError — a typo in a saved template can't break email sending."""
    def __missing__(self, key):
        return '{' + key + '}'


def _get_email_settings():
    settings = EmailSettings.query.get(1)
    if not settings:
        settings = EmailSettings(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings


def _render_email_template(kind, variables):
    """Renders the subject/body for `kind` — the admin-customized template
    if one is saved, otherwise the built-in default. variables is a dict of
    already-display-formatted strings (e.g. due_date as 'YYYY-MM-DD'). Falls
    back to rendering the built-in default if a saved custom template has
    become malformed (e.g. an unclosed brace), so a bad template can never
    crash a reminder send — /admin/emails also validates on save to catch
    this before it's ever stored."""
    settings = EmailSettings.query.get(1)
    default = EMAIL_TEMPLATE_KINDS[kind]
    subject_tpl = (getattr(settings, f'{kind}_subject', None) if settings else None) or default['subject']
    body_tpl = (getattr(settings, f'{kind}_body', None) if settings else None) or default['body']
    safe_vars = _SafeFormatDict(variables)
    try:
        return subject_tpl.format_map(safe_vars), body_tpl.format_map(safe_vars)
    except (ValueError, IndexError) as e:
        logger.error('Malformed custom email template for %s, falling back to default: %s', kind, e)
        return default['subject'].format_map(safe_vars), default['body'].format_map(safe_vars)


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


def _log_activity(action, summary, site_id=None, ticket_id=None):
    """
    Records an admin-side mutation. Never commits itself — call this before
    the route's own db.session.commit() so the log entry and the action it
    describes are always atomic. site_id is best-effort; leave it None for
    anything without one clear site (Users/Sites CRUD, a multi-site bulk import).
    ticket_id is set only by ticket_* actions — it's what powers the per-ticket
    History panel on the ticket detail page (a plain-text search over summary
    would be fragile; this is a real indexed FK instead).
    """
    actor_type, actor_label, actor_user_id = _current_actor()
    db.session.add(ActivityLog(
        actor_type=actor_type, actor_label=actor_label, actor_user_id=actor_user_id,
        site_id=site_id, ticket_id=ticket_id, action=action, summary=summary,
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
        'tickets': user.can_tickets,
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
    'favicon_url': None,
    'app_name': None,
    'logo_background': None,
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
        branding['logo_background'] = settings.logo_background or None
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
    if settings and settings.favicon_filename:
        branding['favicon_url'] = url_for('branding_logo', filename=settings.favicon_filename)

    return branding


# Maps a request path to the top-level nav tab it belongs to (longest-prefix
# match wins, so e.g. '/admin/registry' beats the generic '/admin' fallback).
# Used to highlight the active tab and pick which section's sub-nav to show.
NAV_SECTION_PREFIXES = [
    ('/admin/registry', 'devices'),
    ('/admin/assets', 'devices'),
    ('/admin/bulk_assign', 'devices'),
    ('/admin/bulk_print', 'devices'),
    ('/admin/audit', 'devices'),
    ('/admin/fees', 'devices'),
    ('/admin/repair_categories', 'devices'),
    ('/admin/collection', 'devices'),
    ('/asset_history', 'devices'),
    ('/api/assets', 'devices'),
    ('/admin/device_models', 'devices'),
    ('/admin/asset_number_ranges', 'devices'),
    ('/admin/orphans', 'devices'),
    ('/admin/scan_lookup', 'devices'),
    ('/admin/upload_csv', 'devices'),
    ('/admin/people', 'people'),
    ('/loaner_checkinout', 'loaners'),
    ('/loaner_checkout', 'loaners'),
    ('/loaner_checkin', 'loaners'),
    ('/admin/loaners', 'loaners'),
    ('/admin/repairs', 'repairs'),
    ('/admin/tickets', 'tickets'),
    ('/admin/ticket_categories', 'tickets'),
    ('/submit_ticket', 'tickets'),
    ('/admin/kiosk', 'admin'),
    ('/admin/reminders', 'admin'),
    ('/admin/activity', 'admin'),
    ('/admin/users', 'admin'),
    ('/admin/sites', 'admin'),
    ('/admin/branding', 'admin'),
    ('/admin/emails', 'admin'),
    ('/admin/google_setup', 'admin'),
    ('/admin/custom_fields', 'admin'),
    ('/admin/google_org_units', 'admin'),
    ('/admin/google_ou_push', 'admin'),
    ('/admin/google_field_mapping', 'admin'),
    ('/admin', 'admin'),
    ('/checkin', 'home'),
    ('/checkout', 'home'),
    ('/report_problem', 'home'),
    ('/', 'home'),
]


def _active_nav_section():
    """Longest-prefix match of request.path against NAV_SECTION_PREFIXES.
    None means no top tab should be highlighted (e.g. /admin/search)."""
    path = request.path
    best = None
    for prefix, section in NAV_SECTION_PREFIXES:
        matches = path == prefix or (prefix != '/' and path.startswith(prefix.rstrip('/') + '/'))
        if matches and (best is None or len(prefix) > len(best[0])):
            best = (prefix, section)
    return best[1] if best else None


@app.context_processor
def inject_permission_helper():
    """Exposes can('people'|'devices'|'loaners'|'repairs') to every template, so
    nav links and buttons can hide themselves for users without that permission
    instead of just bouncing them back with an error after they click. Also
    exposes site-scope helpers so templates can hide site columns/filters for
    single-site users and gate Sites management to super admins.

    nav_overdue_count/nav_orphan_count/nav_open_tickets_count power the small
    badges on the nav tabs — only computed for a logged-in admin session (not
    kiosk-only visitors, who never see those tabs), and only when the relevant
    permission is held, so this doesn't add queries to every page load.

    active_section drives which top tab is highlighted and which section's
    sub-nav row renders — computed for every request (cheap, no DB query)."""
    nav_overdue_count = 0
    nav_orphan_count = 0
    nav_open_tickets_count = 0
    if session.get('admin_logged_in'):
        if _has_permission('admin'):
            nav_overdue_count = len(_overdue_assignments(_current_site_ids()))
        if session.get('is_super_admin'):
            nav_orphan_count = Asset.query.filter_by(is_valid=False).count()
        if _has_permission('tickets'):
            nav_open_tickets_count = _scope_tickets(Ticket.query, _current_site_ids()) \
                .filter(Ticket.status.in_(['open', 'in_progress'])).count()
    return {
        'can': _has_permission,
        'is_super_admin': lambda: bool(session.get('is_super_admin')),
        'current_site_ids': _current_site_ids,
        'nav_overdue_count': nav_overdue_count,
        'nav_orphan_count': nav_orphan_count,
        'nav_open_tickets_count': nav_open_tickets_count,
        'branding': _current_branding(),
        'active_section': _active_nav_section(),
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
    open_tickets_count = None
    if _has_permission('tickets'):
        open_tickets_count = _scope_tickets(Ticket.query, site_ids) \
            .filter(Ticket.status.in_(['open', 'in_progress'])).count()

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
                           open_tickets_count=open_tickets_count,
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


def _active_device_models():
    return DeviceModel.query.filter_by(is_active=True).order_by(DeviceModel.manufacturer, DeviceModel.model_name).all()


@app.route('/admin/registry/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_new():
    """Manually adds a single device. Leave asset_tag blank to self-assign one —
    either a random 6-digit tag (default), or the next unused number in a
    specific reserved range if one's picked from the "Generate From Range"
    dropdown (matches a physical batch of pre-printed labels: grab the next
    sticker in the stack, in order, rather than a random one)."""
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    device_models = _active_device_models()
    asset_number_ranges = AssetNumberRange.query.order_by(AssetNumberRange.range_start).all()
    if request.method == 'POST':
        tag = request.form.get('asset_tag', '').strip()
        range_id = request.form.get('range_id', type=int)
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        device_model_id = request.form.get('device_model_id', type=int)
        site_id = request.form.get('site_id', type=int)
        purchase_date = _parse_date(request.form.get('purchase_date'))
        purchase_cost = _parse_money(request.form.get('purchase_cost'))
        warranty_expiration = _parse_date(request.form.get('warranty_expiration'))

        if not serial:
            flash('Serial number is required.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if tag and AssetRegistry.query.filter_by(asset_tag=tag).first():
            flash(f'Asset tag "{tag}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if AssetRegistry.query.filter_by(serial_number=serial).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if not tag:
            existing_tags = {t for (t,) in db.session.query(AssetRegistry.asset_tag).all()}
            if range_id:
                asset_range = AssetNumberRange.query.get(range_id)
                if not asset_range:
                    flash('That reserved range no longer exists.', 'error')
                    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)
                tag = _next_tag_in_range(existing_tags, asset_range.range_start, asset_range.range_end)
                if not tag:
                    flash(f'"{asset_range.label}" is fully used — every number from {asset_range.range_start} to {asset_range.range_end} is already in the registry.', 'error')
                    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)
            else:
                tag = _generate_asset_tag(existing_tags)

        try:
            db.session.add(AssetRegistry(
                asset_tag=tag, serial_number=serial,
                description=description, device_type=device_type, device_model_id=device_model_id, site_id=site_id,
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
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add device: {e}', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

    prefill_tag = request.args.get('asset_tag', '').strip()
    prefill_form = {'asset_tag': prefill_tag} if prefill_tag else None
    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=prefill_form, sites=sites)


@app.route('/admin/registry/quick_add', methods=['POST'])
@require_permission('devices_manage')
def admin_registry_quick_add():
    """Bulk-intake flow for unboxing a shipment: scan a serial, hit enter,
    repeat — creates a minimal AssetRegistry row (asset tag auto-generated)
    and redirects right back to the registry list, never to the full Add
    Device form, so a USB barcode scanner (which types the serial + Enter)
    can just keep firing without the admin touching anything between scans.
    Device type and site are carried back via querystring so the dropdowns
    stay put for the next scan instead of resetting to their defaults."""
    site_ids = _current_site_ids()
    serial = request.form.get('serial_number', '').strip()
    device_type = request.form.get('device_type', 'chromebook').strip()
    device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
    site_id = request.form.get('site_id', type=int)
    sticky = {'quick_device_type': device_type, 'quick_site_id': site_id}

    if not serial:
        flash('Scan or type a serial number.', 'error')
        return redirect(url_for('admin_registry', **sticky))
    if site_ids is not None and (not site_id or site_id not in site_ids):
        flash('Choose one of your own sites.', 'error')
        return redirect(url_for('admin_registry'))
    if AssetRegistry.query.filter_by(serial_number=serial).first():
        flash(f'A device with serial number "{serial}" already exists.', 'error')
        return redirect(url_for('admin_registry', **sticky))

    existing_tags = {t for (t,) in db.session.query(AssetRegistry.asset_tag).all()}
    tag = _generate_asset_tag(existing_tags)
    try:
        db.session.add(AssetRegistry(asset_tag=tag, serial_number=serial, device_type=device_type, site_id=site_id))
        _log_activity('device_add', f'Quick-added device {tag} (serial {serial}) to the registry.', site_id=site_id)
        db.session.commit()
        orphan = Asset.query.filter_by(asset_tag=tag, is_valid=False).first()
        if orphan:
            orphan.is_valid = True
            db.session.commit()
        flash(f'Added {tag} (serial {serial}) — ready for the next scan.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Could not add device: that asset tag or serial number is already in use.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not add device: {e}', 'error')
    return redirect(url_for('admin_registry', **sticky))


@app.route('/admin/registry/<string:asset_tag>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_edit(asset_tag):
    """Edits a device's attributes (serial, description, type, site). The
    asset_tag itself isn't editable here — it's the key used everywhere
    else (assignments, history, events), so renaming it is out of scope."""
    site_ids = _current_site_ids()
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first_or_404()
    sites = _sites_for_actor(site_ids)
    device_models = _active_device_models()

    if request.method == 'POST':
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        device_model_id = request.form.get('device_model_id', type=int)
        site_id = request.form.get('site_id', type=int)
        purchase_date = _parse_date(request.form.get('purchase_date'))
        purchase_cost = _parse_money(request.form.get('purchase_cost'))
        warranty_expiration = _parse_date(request.form.get('warranty_expiration'))

        if not serial:
            flash('Serial number is required.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)

        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)

        if AssetRegistry.query.filter(AssetRegistry.serial_number == serial,
                                       AssetRegistry.asset_tag != asset_tag).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)

        try:
            registry_row.serial_number = serial
            registry_row.description = description
            registry_row.device_type = device_type
            registry_row.device_model_id = device_model_id
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

    return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)


# ─── Device Model Catalog ──────────────────────────────────────────────────────

DEVICE_MODEL_SORT_COLUMNS = {
    'manufacturer': (DeviceModel.manufacturer, DeviceModel.model_name),
    'model_name': (DeviceModel.model_name,),
    'device_type': (DeviceModel.device_type,),
}


@app.route('/admin/device_models')
@require_permission('devices_manage')
def admin_device_models():
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'manufacturer').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in DEVICE_MODEL_SORT_COLUMNS:
        sort = 'manufacturer'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = DeviceModel.query
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(DeviceModel.manufacturer.ilike(like), DeviceModel.model_name.ilike(like),
                                     DeviceModel.notes.ilike(like)))
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in DEVICE_MODEL_SORT_COLUMNS[sort]]
    models = query.order_by(*order_exprs).all()
    return render_template('admin_device_models.html', models=models, search=search, sort=sort, sort_dir=sort_dir)


@app.route('/admin/device_models/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_device_model_new():
    if request.method == 'POST':
        manufacturer = request.form.get('manufacturer', '').strip()
        model_name = request.form.get('model_name', '').strip()
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        notes = request.form.get('notes', '').strip() or None

        if not manufacturer or not model_name:
            flash('Manufacturer and model name are required.', 'error')
            return render_template('admin_device_model_form.html', model=None, device_types=DEVICE_TYPES, form=request.form)
        if DeviceModel.query.filter(db.func.lower(DeviceModel.manufacturer) == manufacturer.lower(),
                                     db.func.lower(DeviceModel.model_name) == model_name.lower()).first():
            flash(f'"{manufacturer} {model_name}" already exists.', 'error')
            return render_template('admin_device_model_form.html', model=None, device_types=DEVICE_TYPES, form=request.form)

        model = DeviceModel(manufacturer=manufacturer, model_name=model_name, device_type=device_type, notes=notes)
        db.session.add(model)
        _log_activity('device_model_add', f'Added device model "{manufacturer} {model_name}".')
        db.session.commit()
        flash(f'Added "{manufacturer} {model_name}".', 'success')
        return redirect(url_for('admin_device_models'))

    return render_template('admin_device_model_form.html', model=None, device_types=DEVICE_TYPES, form=None)


@app.route('/admin/device_models/<int:model_id>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_device_model_edit(model_id):
    model = DeviceModel.query.get_or_404(model_id)
    if request.method == 'POST':
        manufacturer = request.form.get('manufacturer', '').strip()
        model_name = request.form.get('model_name', '').strip()
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        notes = request.form.get('notes', '').strip() or None
        is_active = bool(request.form.get('is_active'))

        if not manufacturer or not model_name:
            flash('Manufacturer and model name are required.', 'error')
            return render_template('admin_device_model_form.html', model=model, device_types=DEVICE_TYPES, form=None)
        if DeviceModel.query.filter(db.func.lower(DeviceModel.manufacturer) == manufacturer.lower(),
                                     db.func.lower(DeviceModel.model_name) == model_name.lower(),
                                     DeviceModel.id != model_id).first():
            flash(f'"{manufacturer} {model_name}" already exists.', 'error')
            return render_template('admin_device_model_form.html', model=model, device_types=DEVICE_TYPES, form=None)

        model.manufacturer = manufacturer
        model.model_name = model_name
        model.device_type = device_type
        model.notes = notes
        model.is_active = is_active
        _log_activity('device_model_edit', f'Edited device model "{manufacturer} {model_name}".')
        db.session.commit()
        flash(f'Updated "{manufacturer} {model_name}".', 'success')
        return redirect(url_for('admin_device_models'))

    return render_template('admin_device_model_form.html', model=model, device_types=DEVICE_TYPES, form=None)


@app.route('/admin/device_models/<int:model_id>/delete', methods=['POST'])
@require_permission('devices_manage')
def admin_device_model_delete(model_id):
    model = DeviceModel.query.get_or_404(model_id)
    in_use = AssetRegistry.query.filter_by(device_model_id=model_id).count()
    if in_use:
        flash(f'Cannot delete "{model.full_name}" — {in_use} device(s) still reference it.', 'error')
        return redirect(url_for('admin_device_models'))
    label = model.full_name
    db.session.delete(model)
    _log_activity('device_model_delete', f'Deleted device model "{label}".')
    db.session.commit()
    flash(f'Deleted "{label}".', 'success')
    return redirect(url_for('admin_device_models'))


# ─── Reserved Asset-Tag Ranges ─────────────────────────────────────────────────

ASSET_NUMBER_RANGE_SORT_COLUMNS = {
    'label': (AssetNumberRange.label,),
    'range_start': (AssetNumberRange.range_start,),
}


@app.route('/admin/asset_number_ranges')
@require_permission('devices_manage')
def admin_asset_number_ranges():
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'range_start').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in ASSET_NUMBER_RANGE_SORT_COLUMNS:
        sort = 'range_start'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = AssetNumberRange.query
    if search:
        query = query.filter(AssetNumberRange.label.ilike(f'%{search}%'))
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()) for c in ASSET_NUMBER_RANGE_SORT_COLUMNS[sort]]
    ranges = query.order_by(*order_exprs).all()
    return render_template('admin_asset_number_ranges.html', ranges=ranges, search=search, sort=sort, sort_dir=sort_dir)


def _parse_asset_range_bounds(form):
    """Returns (range_start, range_end, error) — error is a flashable string, or None if valid."""
    start = form.get('range_start', type=int)
    end = form.get('range_end', type=int)
    if start is None or end is None or start < 0 or end < 0:
        return None, None, 'Start and end must be positive whole numbers.'
    if start > end:
        return None, None, 'Start must be less than or equal to end.'
    return start, end, None


@app.route('/admin/asset_number_ranges/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_asset_number_range_new():
    if request.method == 'POST':
        label = request.form.get('label', '').strip()
        notes = request.form.get('notes', '').strip() or None
        start, end, error = _parse_asset_range_bounds(request.form)

        if not label:
            flash('Label is required.', 'error')
            return render_template('admin_asset_number_range_form.html', range=None, form=request.form)
        if error:
            flash(error, 'error')
            return render_template('admin_asset_number_range_form.html', range=None, form=request.form)

        db.session.add(AssetNumberRange(label=label, range_start=start, range_end=end, notes=notes))
        _log_activity('asset_number_range_add', f'Reserved asset tag range "{label}" ({start}-{end}).')
        db.session.commit()
        flash(f'Reserved range "{label}" ({start}-{end}).', 'success')
        return redirect(url_for('admin_asset_number_ranges'))

    return render_template('admin_asset_number_range_form.html', range=None, form=None)


@app.route('/admin/asset_number_ranges/<int:range_id>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_asset_number_range_edit(range_id):
    asset_range = AssetNumberRange.query.get_or_404(range_id)
    if request.method == 'POST':
        label = request.form.get('label', '').strip()
        notes = request.form.get('notes', '').strip() or None
        start, end, error = _parse_asset_range_bounds(request.form)

        if not label:
            flash('Label is required.', 'error')
            return render_template('admin_asset_number_range_form.html', range=asset_range, form=None)
        if error:
            flash(error, 'error')
            return render_template('admin_asset_number_range_form.html', range=asset_range, form=None)

        asset_range.label = label
        asset_range.range_start = start
        asset_range.range_end = end
        asset_range.notes = notes
        _log_activity('asset_number_range_edit', f'Edited reserved range "{label}" ({start}-{end}).')
        db.session.commit()
        flash(f'Updated range "{label}".', 'success')
        return redirect(url_for('admin_asset_number_ranges'))

    return render_template('admin_asset_number_range_form.html', range=asset_range, form=None)


@app.route('/admin/asset_number_ranges/<int:range_id>/set_default', methods=['POST'])
@require_permission('devices_manage')
def admin_asset_number_range_set_default(range_id):
    """Toggles this range as THE default _generate_asset_tag() draws from —
    at most one range is ever default, so setting one clears any other."""
    asset_range = AssetNumberRange.query.get_or_404(range_id)
    if asset_range.is_default:
        asset_range.is_default = False
        _log_activity('asset_number_range_edit', f'Unset "{asset_range.label}" as the default range.')
        flash(f'"{asset_range.label}" is no longer the default range.', 'success')
    else:
        AssetNumberRange.query.filter(AssetNumberRange.id != range_id).update({'is_default': False})
        asset_range.is_default = True
        _log_activity('asset_number_range_edit', f'Set "{asset_range.label}" as the default range.')
        flash(f'"{asset_range.label}" is now the default range — Add Device and CSV import will pull from it automatically.', 'success')
    db.session.commit()
    return redirect(url_for('admin_asset_number_ranges'))


@app.route('/admin/asset_number_ranges/<int:range_id>/delete', methods=['POST'])
@require_permission('devices_manage')
def admin_asset_number_range_delete(range_id):
    asset_range = AssetNumberRange.query.get_or_404(range_id)
    label = asset_range.label
    db.session.delete(asset_range)
    _log_activity('asset_number_range_delete', f'Deleted reserved range "{label}".')
    db.session.commit()
    flash(f'Deleted range "{label}".', 'success')
    return redirect(url_for('admin_asset_number_ranges'))


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


REGISTRY_SORT_COLUMNS = {
    'asset_tag': AssetRegistry.asset_tag,
    'serial_number': AssetRegistry.serial_number,
    'description': AssetRegistry.description,
    'device_type': AssetRegistry.device_type,
    'site': Site.name,
    'status': Asset.status,
}


@app.route('/admin/registry')
@require_permission('devices')
def admin_registry():
    page          = request.args.get('page', 1, type=int)
    per_page      = 50
    site_ids      = _current_site_ids()
    query         = _scope_registry(AssetRegistry.query, site_ids)
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

    sort = request.args.get('sort', 'asset_tag').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in REGISTRY_SORT_COLUMNS:
        sort = 'asset_tag'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort == 'site':
        query = query.outerjoin(Site, AssetRegistry.site_id == Site.id)
    elif sort == 'status':
        query = query.outerjoin(Asset, Asset.asset_tag == AssetRegistry.asset_tag)
    sort_col = REGISTRY_SORT_COLUMNS[sort]
    order_expr = sort_col.desc() if sort_dir == 'desc' else sort_col.asc()
    # nulls last regardless of direction — an empty serial/description/site
    # shouldn't dominate either end of the sort
    query = query.order_by(order_expr.nullslast(), AssetRegistry.asset_tag)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    page_tags = [row.asset_tag for row in pagination.items]
    assets_by_tag = {
        a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(page_tags))
    }

    quick_device_type = request.args.get('quick_device_type', 'chromebook').strip()
    quick_device_type = quick_device_type if quick_device_type in DEVICE_TYPES else 'chromebook'
    quick_site_id = request.args.get('quick_site_id', type=int)
    return render_template('admin_registry.html', pagination=pagination, search=search,
                           status_filter=status_filter, asset_statuses=ASSET_STATUSES,
                           type_filter=type_filter, device_types=DEVICE_TYPES,
                           sort=sort, sort_dir=sort_dir,
                           person_filter=person_filter, person_filter_name=person_filter_name,
                           warranty_filter=warranty_filter, today=datetime.utcnow().date(),
                           assets_by_tag=assets_by_tag, sites=_sites_for_actor(site_ids),
                           quick_device_type=quick_device_type, quick_site_id=quick_site_id)


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
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = Asset.query.filter_by(is_valid=False)
    if search:
        query = query.filter(Asset.asset_tag.ilike(f'%{search}%'))
    orphans = query.order_by(Asset.asset_tag.desc() if sort_dir == 'desc' else Asset.asset_tag.asc()).all()
    return render_template('admin_orphans.html', orphans=orphans, search=search, sort_dir=sort_dir)


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


PEOPLE_SORT_COLUMNS = {
    'name': (Person.last_name, Person.first_name),
    'email': (Person.email,),
    'external_id': (Person.external_id,),
    'role': (Person.role,),
    'site': (Site.name,),
    'department': (Person.department,),
}


@app.route('/admin/people')
@require_permission('people')
def admin_people():
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    show     = request.args.get('show', 'active')  # 'active' | 'inactive' | 'all'
    query    = _scope_people(Person.query, _current_site_ids())
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

    sort = request.args.get('sort', 'name').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in PEOPLE_SORT_COLUMNS:
        sort = 'name'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort == 'site':
        query = query.outerjoin(Site, Person.site_id == Site.id)
    sort_cols = PEOPLE_SORT_COLUMNS[sort]
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]
    query = query.order_by(*order_exprs, Person.last_name, Person.first_name)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    site_ids = _current_site_ids()
    quick_role = request.args.get('quick_role', 'student').strip()
    quick_role = quick_role if quick_role in ('staff', 'student') else 'student'
    quick_site_id = request.args.get('quick_site_id', type=int)
    return render_template('admin_people.html', pagination=pagination, search=search, show=show,
                           insurance_filter=insurance_filter, sort=sort, sort_dir=sort_dir,
                           sites=_sites_for_actor(site_ids), quick_role=quick_role, quick_site_id=quick_site_id)


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


@app.route('/admin/people/quick_add', methods=['POST'])
@require_permission('people')
def admin_people_quick_add():
    """Bulk-intake flow for entering a roster by hand without leaving the
    People list — type name/email, submit, land right back on the same page
    with the next entry's first field ready to go. Role and site are
    carried back via querystring so they stay put between entries, since a
    batch is usually all the same role/site. Reuses the same
    validation/creation logic as the full Add Person form."""
    site_ids = _current_site_ids()
    values = _person_form_values()
    sticky = {'quick_role': values['role'], 'quick_site_id': values['site_id']}
    error = _validate_person_form(values, allowed_site_ids=site_ids)
    if error:
        flash(error, 'error')
        return redirect(url_for('admin_people', **sticky))

    try:
        person = Person(**values)
        db.session.add(person)
        _log_activity('person_add', f'Quick-added {person.full_name}.', site_id=values.get('site_id'))
        db.session.commit()
        flash(f'Added {person.full_name} — ready for the next entry.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Could not add person: that email or ID number is already in use.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not add person: {e}', 'error')
    return redirect(url_for('admin_people', **sticky))


@app.route('/admin/people/<int:person_id>/edit', methods=['GET', 'POST'])
@require_permission('people')
def admin_person_edit(person_id):
    site_ids = _current_site_ids()
    person = _scope_people(Person.query, site_ids).filter_by(id=person_id).first_or_404()
    custom_field_labels = {f.field_key: f.label for f in CustomField.query.filter_by(entity_type='person').all()}

    if request.method == 'POST':
        values = _person_form_values()
        error = _validate_person_form(values, person_id=person_id, allowed_site_ids=site_ids)
        if error:
            flash(error, 'error')
            return render_template('admin_person_form.html', person=person, form=values,
                                    sites=_sites_for_actor(site_ids), custom_field_labels=custom_field_labels)

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
                                    sites=_sites_for_actor(site_ids), custom_field_labels=custom_field_labels)

    return render_template('admin_person_form.html', person=person, form=None,
                           sites=_sites_for_actor(site_ids), custom_field_labels=custom_field_labels)


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

    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    if registry_row and registry_row.is_loaner:
        return 'error', (f'{asset_tag} is in the loaner pool and can\'t be permanently assigned — '
                          'check it out as a loaner instead, or remove it from the loaner pool first.')

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
        _log_activity('device_assign', f'Assigned {asset_tag} to {person.full_name}.',
                       site_id=registry_row.site_id if registry_row else None)
        db.session.commit()
        if registry_row:
            _sync_device_google_state(registry_row, enabled=True, person=person)
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

    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    custom_field_labels = {f.field_key: f.label for f in CustomField.query.filter_by(entity_type='device').all()}

    return render_template('admin_assign.html', registry_row=registry_row, asset=asset, has_people=has_people,
                           history=history, combined_history=combined_history, events=events, incidents=incidents,
                           current_person_incident_count=current_person_incident_count,
                           repair_categories=repair_categories,
                           open_repair=open_repair, closed_repairs=closed_repairs, repair_outcomes=REPAIR_OUTCOMES,
                           asset_statuses=ASSET_STATUSES, custom_field_labels=custom_field_labels,
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


def _slugify_field_key(label):
    """Turns a human label like 'Employee ID' into a safe JSON-key/slug like
    'employee_id' — lowercase, non-alphanumerics collapsed to underscores,
    trimmed. Used so custom fields don't need a separate raw-key input."""
    slug = re.sub(r'[^a-z0-9]+', '_', label.strip().lower()).strip('_')
    return slug or 'field'


@app.route('/admin/custom_fields')
@require_super_admin
def admin_custom_fields():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    fields = CustomField.query.filter_by(entity_type=entity_type).order_by(CustomField.label).all()
    return render_template('admin_custom_fields.html', fields=fields, entity_type=entity_type)


@app.route('/admin/custom_fields/new', methods=['GET', 'POST'])
@require_super_admin
def admin_custom_field_new():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    if request.method == 'POST':
        entity_type = request.form.get('entity_type', entity_type)
        label = request.form.get('label', '').strip()
        field_type = request.form.get('field_type', 'text').strip()
        if field_type not in ('text', 'number', 'date', 'boolean', 'email'):
            field_type = 'text'
        if not label:
            flash('Give the field a label.', 'error')
            return render_template('admin_custom_field_form.html', entity_type=entity_type, form=request.form)

        field_key = _slugify_field_key(label)
        if CustomField.query.filter_by(entity_type=entity_type, field_key=field_key).first():
            flash(f'A field with key "{field_key}" already exists for this entity type.', 'error')
            return render_template('admin_custom_field_form.html', entity_type=entity_type, form=request.form)

        db.session.add(CustomField(entity_type=entity_type, field_key=field_key, label=label, field_type=field_type))
        _log_activity('custom_field_add', f'Added custom field "{label}" ({entity_type}).')
        db.session.commit()
        flash(f'Field "{label}" added.', 'success')
        return redirect(url_for('admin_custom_fields', entity=entity_type))

    return render_template('admin_custom_field_form.html', entity_type=entity_type, form=None)


@app.route('/admin/custom_fields/<int:field_id>/delete', methods=['POST'])
@require_super_admin
def admin_custom_field_delete(field_id):
    """Deletes a custom field's definition and any mappings that fed it —
    existing values already written into rows' custom_fields JSON are left
    alone (harmless orphaned data, not worth a bulk cleanup pass)."""
    field = CustomField.query.get_or_404(field_id)
    target = f'custom:{field.field_key}'
    GoogleFieldMapping.query.filter_by(entity_type=field.entity_type, target_field=target).delete()
    _log_activity('custom_field_delete', f'Deleted custom field "{field.label}" ({field.entity_type}).')
    db.session.delete(field)
    db.session.commit()
    flash('Field deleted.', 'success')
    return redirect(url_for('admin_custom_fields', entity=field.entity_type))


@app.route('/admin/google_org_units')
@require_super_admin
def admin_google_org_units():
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    sites = Site.query.order_by(Site.name).all()
    return render_template('admin_google_org_units.html', org_units=org_units, sites=sites,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/google_org_units/refresh', methods=['POST'])
@require_super_admin
def admin_google_org_units_refresh():
    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet — see /admin/google_setup.', 'info')
        return redirect(url_for('admin_google_org_units'))
    try:
        count = _fetch_google_org_units()
        _log_activity('org_unit_refresh', f'Refreshed org unit list from Google: {count} found.')
        flash(f'Pulled {count} org unit(s) from Google Workspace.', 'success')
    except Exception as e:
        flash(f'Refresh failed: {e}', 'error')
    return redirect(url_for('admin_google_org_units'))


@app.route('/admin/google_org_units/save', methods=['POST'])
@require_super_admin
def admin_google_org_units_save():
    valid_site_ids = {s.id for s in Site.query.all()}
    changed = 0
    for ou in GoogleOrgUnit.query.all():
        category = request.form.get(f'category_{ou.id}', 'unclassified')
        if category not in ('unclassified', 'staff', 'student'):
            category = 'unclassified'
        if category != ou.category:
            ou.category = category
            changed += 1

        site_raw = request.form.get(f'site_{ou.id}', '').strip()
        site_id = int(site_raw) if site_raw.isdigit() and int(site_raw) in valid_site_ids else None
        if site_id != ou.site_id:
            ou.site_id = site_id
            changed += 1
    if changed:
        _log_activity('org_unit_classify', f'Reclassified/re-sited {changed} org unit field(s).')
        db.session.commit()
        flash(f'Saved — {changed} change(s).', 'success')
    else:
        flash('No changes to save.', 'info')
    return redirect(url_for('admin_google_org_units'))


@app.route('/admin/google_ou_push')
@require_super_admin
def admin_google_ou_push():
    """The reverse of Org Units' site-tagging: here FoxDesk's own Site
    decides where a site's loaner Chromebooks belong in Google, and this
    page pushes them there — see _push_loaners_to_ou(). Deliberately
    devices-only: FoxDesk doesn't push people's Google accounts between org
    units, only reads them (see _run_google_people_sync/_org_unit_site_id)."""
    sites = Site.query.order_by(Site.name).all()
    loaner_counts = {
        row[0]: row[1] for row in db.session.query(AssetRegistry.site_id, db.func.count(AssetRegistry.id))
        .filter(AssetRegistry.is_loaner.is_(True), AssetRegistry.serial_number.isnot(None))
        .group_by(AssetRegistry.site_id)
    }
    return render_template('admin_google_ou_push.html', sites=sites, loaner_counts=loaner_counts,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/google_ou_push/loaners/<int:site_id>', methods=['POST'])
@require_super_admin
def admin_google_ou_push_loaners(site_id):
    site = Site.query.get_or_404(site_id)
    if not site.loaner_org_unit_path:
        flash(f'Set a Loaner Org Unit on {site.name} first (under Sites) before pushing.', 'error')
        return redirect(url_for('admin_google_ou_push'))
    try:
        moved, not_found = _push_loaners_to_ou(site)
        _log_activity('loaner_ou_push', f'Pushed {moved} loaner(s) from {site.name} to {site.loaner_org_unit_path}.',
                       site_id=site.id)
        msg = f'Moved {moved} device(s) to {site.loaner_org_unit_path}.'
        if not_found:
            msg += f' {not_found} loaner(s) had no matching Chrome device in Google (e.g. a charger, not a Chromebook).'
        flash(msg, 'success' if moved else 'info')
    except Exception as e:
        flash(f'Push failed: {e}', 'error')
    return redirect(url_for('admin_google_ou_push'))


@app.route('/admin/google_field_mapping')
@require_super_admin
def admin_google_field_mapping():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    mappings = GoogleFieldMapping.query.filter_by(entity_type=entity_type).order_by(GoogleFieldMapping.google_field).all()
    custom_fields = CustomField.query.filter_by(entity_type=entity_type).order_by(CustomField.label).all()
    real_fields = PERSON_SYNC_TARGET_FIELDS if entity_type == 'person' else DEVICE_SYNC_TARGET_FIELDS
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    return render_template('admin_google_field_mapping.html', mappings=mappings, entity_type=entity_type,
                           custom_fields=custom_fields, real_fields=real_fields,
                           org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES,
                           google_sync_enabled=GOOGLE_SYNC_ENABLED)


@app.route('/admin/google_field_mapping/new', methods=['GET', 'POST'])
@require_super_admin
def admin_google_field_mapping_new():
    entity_type = request.args.get('entity', 'person')
    if entity_type not in ('person', 'device'):
        entity_type = 'person'
    custom_fields = CustomField.query.filter_by(entity_type=entity_type).order_by(CustomField.label).all()
    real_fields = PERSON_SYNC_TARGET_FIELDS if entity_type == 'person' else DEVICE_SYNC_TARGET_FIELDS
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()

    if request.method == 'POST':
        entity_type = request.form.get('entity_type', entity_type)
        google_field = request.form.get('google_field', '').strip()
        target_field = request.form.get('target_field', '').strip()
        org_unit_scope = request.form.get('org_unit_scope', '').strip() or None
        valid_targets = set((PERSON_SYNC_TARGET_FIELDS if entity_type == 'person' else DEVICE_SYNC_TARGET_FIELDS).keys())
        valid_targets |= {f'custom:{c.field_key}' for c in CustomField.query.filter_by(entity_type=entity_type).all()}
        valid_scopes = set(ORG_UNIT_SCOPE_CHOICES.keys()) | {ou.org_unit_path for ou in org_units}

        if not google_field:
            flash('Enter the Google field to pull from (e.g. orgUnitPath).', 'error')
            return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                                   custom_fields=custom_fields, real_fields=real_fields,
                                   org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=request.form)
        if target_field not in valid_targets:
            flash('Choose a valid target field.', 'error')
            return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                                   custom_fields=custom_fields, real_fields=real_fields,
                                   org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=request.form)
        if org_unit_scope is not None and org_unit_scope not in valid_scopes:
            flash('Choose a valid org unit scope.', 'error')
            return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                                   custom_fields=custom_fields, real_fields=real_fields,
                                   org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=request.form)

        db.session.add(GoogleFieldMapping(entity_type=entity_type, google_field=google_field,
                                           target_field=target_field, org_unit_scope=org_unit_scope))
        scope_note = f' (scoped to {org_unit_scope})' if org_unit_scope else ''
        _log_activity('google_field_mapping_add', f'Mapped Google "{google_field}" -> "{target_field}" ({entity_type}){scope_note}.')
        db.session.commit()
        flash('Mapping added.', 'success')
        return redirect(url_for('admin_google_field_mapping', entity=entity_type))

    return render_template('admin_google_field_mapping_form.html', entity_type=entity_type,
                           custom_fields=custom_fields, real_fields=real_fields,
                           org_units=org_units, org_unit_scope_choices=ORG_UNIT_SCOPE_CHOICES, form=None)


@app.route('/admin/google_field_mapping/<int:mapping_id>/delete', methods=['POST'])
@require_super_admin
def admin_google_field_mapping_delete(mapping_id):
    mapping = GoogleFieldMapping.query.get_or_404(mapping_id)
    entity_type = mapping.entity_type
    _log_activity('google_field_mapping_delete', f'Removed mapping "{mapping.google_field}" -> "{mapping.target_field}" ({entity_type}).')
    db.session.delete(mapping)
    db.session.commit()
    flash('Mapping removed.', 'success')
    return redirect(url_for('admin_google_field_mapping', entity=entity_type))


@app.route('/admin/google_field_mapping/sync/people', methods=['POST'])
@require_super_admin
def admin_google_sync_people():
    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet — see /admin/google_setup.', 'info')
        return redirect(url_for('admin_google_field_mapping', entity='person'))
    try:
        matched, updated, unmatched = _run_google_people_sync()
        _log_activity('google_field_sync', f'Synced People from Google: {matched} matched, {updated} updated.')
        flash(f'{matched} matched, {updated} updated. {unmatched} Google account(s) had no matching Person by email.',
              'success' if matched else 'info')
    except Exception as e:
        flash(f'Sync failed: {e}', 'error')
    return redirect(url_for('admin_google_field_mapping', entity='person'))


@app.route('/admin/google_field_mapping/sync/devices', methods=['POST'])
@require_super_admin
def admin_google_sync_devices():
    if not GOOGLE_SYNC_ENABLED:
        flash('Google Workspace sync isn\'t configured yet — see /admin/google_setup.', 'info')
        return redirect(url_for('admin_google_field_mapping', entity='device'))
    try:
        matched, updated, unmatched = _run_google_device_sync()
        _log_activity('google_field_sync', f'Synced Devices from Google: {matched} matched, {updated} updated.')
        flash(f'{matched} matched, {updated} updated. {unmatched} Google device(s) had no matching registry serial number.',
              'success' if matched else 'info')
    except Exception as e:
        flash(f'Sync failed: {e}', 'error')
    return redirect(url_for('admin_google_field_mapping', entity='device'))


# ─── Kiosk Devices ──────────────────────────────────────────────────────────────

@app.route('/admin/kiosk')
@require_permission('admin')
def admin_kiosk():
    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'
    query = KioskDevice.query
    if site_ids is not None:
        query = query.filter(KioskDevice.site_id.in_(site_ids))
    if search:
        query = query.filter(KioskDevice.label.ilike(f'%{search}%'))
    devices = query.order_by(KioskDevice.created_at.asc() if sort_dir == 'asc' else KioskDevice.created_at.desc()).all()
    token = request.cookies.get('kiosk_token')
    current_device = KioskDevice.query.filter_by(token=token).first() if token else None
    return render_template('admin_kiosk.html', devices=devices, current_device=current_device,
                           sites=_sites_for_actor(site_ids), search=search, sort_dir=sort_dir)


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
        'can_tickets': bool(request.form.get('can_tickets')),
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
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = _scope_users(User.query, _current_site_ids())
    if search:
        query = query.filter(User.username.ilike(f'%{search}%'))
    users = query.order_by(User.username.desc() if sort_dir == 'desc' else User.username.asc()).all()
    return render_template('admin_users.html', users=users, search=search, sort_dir=sort_dir)


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

        if action == 'remove_favicon':
            _delete_branding_logo(settings.favicon_filename)
            settings.favicon_filename = None
            _log_activity('branding_edit', 'Removed the favicon.')
            db.session.commit()
            flash('Favicon removed.', 'success')
            return redirect(url_for('admin_branding'))

        app_name = request.form.get('app_name', '').strip()
        primary_color = request.form.get('primary_color', '').strip()
        logo_background = request.form.get('logo_background', '').strip()
        logo_background = logo_background if logo_background in ('light', 'dark') else None

        if not _HEX_RE.match(primary_color):
            flash('Primary color must be a valid hex color (e.g. #c8102e).', 'error')
            return render_template('admin_branding.html', settings=settings)

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), 'global')
            new_favicon = _save_branding_logo(request.files.get('favicon'), 'favicon')
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_branding.html', settings=settings)

        settings.app_name = app_name or None
        settings.logo_background = logo_background
        if new_logo:
            _delete_branding_logo(settings.logo_filename)
            settings.logo_filename = new_logo
        if new_favicon:
            _delete_branding_logo(settings.favicon_filename)
            settings.favicon_filename = new_favicon

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


def _email_template_sample_vars():
    """Fake-but-realistic values used to validate a saved template and to
    render the live preview on /admin/emails — computed fresh per request
    so the sample due date always reads as "today", not whenever the
    server process happened to start."""
    return {
        'first_name': 'Jordan', 'full_name': 'Jordan Smith', 'asset_tag': '123456',
        'due_date': datetime.utcnow().date().isoformat(), 'days_overdue': '3', 'days_overdue_plural': 's',
    }


@app.route('/admin/emails', methods=['GET', 'POST'])
@require_super_admin
def admin_emails():
    """Lets a super admin rewrite the wording of every system email this app
    sends — loaner reminders (overdue / due-soon / no-due-date) and the
    overdue-assignment reminder — using plain {variable} placeholders.
    Blank fields fall back to the built-in default (EMAIL_TEMPLATE_KINDS)."""
    settings = _get_email_settings()

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action.startswith('reset:'):
            kind = action.split(':', 1)[1]
            if kind in EMAIL_TEMPLATE_KINDS:
                setattr(settings, f'{kind}_subject', None)
                setattr(settings, f'{kind}_body', None)
                _log_activity('email_template_edit', f'Reset "{EMAIL_TEMPLATE_KINDS[kind]["label"]}" email to the built-in default.')
                db.session.commit()
                flash('Reset to the built-in default.', 'success')
            return redirect(url_for('admin_emails'))

        # Validate every submitted template against the sample variables
        # before saving any of them — a typo (e.g. an unclosed brace) gets
        # caught here with a clear error instead of silently breaking a
        # reminder send later.
        submitted = {}
        for kind, default in EMAIL_TEMPLATE_KINDS.items():
            subject = request.form.get(f'{kind}_subject', '').strip()
            body = request.form.get(f'{kind}_body', '').strip()
            safe_vars = _SafeFormatDict(_email_template_sample_vars())
            try:
                if subject:
                    subject.format_map(safe_vars)
                if body:
                    body.format_map(safe_vars)
            except (ValueError, IndexError) as e:
                flash(f'"{default["label"]}": invalid template syntax ({e}). Nothing was saved — fix it and try again.', 'error')
                return redirect(url_for('admin_emails'))
            submitted[kind] = (subject or None, body or None)

        for kind, (subject, body) in submitted.items():
            setattr(settings, f'{kind}_subject', subject)
            setattr(settings, f'{kind}_body', body)
        _log_activity('email_template_edit', 'Updated custom email wording.')
        db.session.commit()
        flash('Email templates updated.', 'success')
        return redirect(url_for('admin_emails'))

    safe_sample = _SafeFormatDict(_email_template_sample_vars())
    kinds = []
    for key, default in EMAIL_TEMPLATE_KINDS.items():
        current_subject = getattr(settings, f'{key}_subject') or default['subject']
        current_body = getattr(settings, f'{key}_body') or default['body']
        kinds.append({
            'key': key, 'label': default['label'],
            'subject': current_subject, 'body': current_body,
            'is_custom': bool(getattr(settings, f'{key}_subject') or getattr(settings, f'{key}_body')),
            'variables': EMAIL_TEMPLATE_VARIABLES[key],
            'preview_subject': current_subject.format_map(safe_sample),
            'preview_body': current_body.format_map(safe_sample),
        })
    return render_template('admin_emails.html', kinds=kinds, email_enabled=EMAIL_ENABLED,
                           sample_vars=_email_template_sample_vars())


# ─── Sites ────────────────────────────────────────────────────────────────────

@app.route('/admin/sites')
@require_super_admin
def admin_sites():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = Site.query
    if search:
        query = query.filter(Site.name.ilike(f'%{search}%'))
    sites = query.order_by(Site.name.desc() if sort_dir == 'desc' else Site.name.asc()).all()
    return render_template('admin_sites.html', sites=sites, search=search, sort_dir=sort_dir)


@app.route('/admin/sites/new', methods=['GET', 'POST'])
@require_super_admin
def admin_site_new():
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Site name is required.', 'error')
            return render_template('admin_site_form.html', site=None, form=request.form, org_units=org_units)
        if Site.query.filter(db.func.lower(Site.name) == name.lower()).first():
            flash(f'A site named "{name}" already exists.', 'error')
            return render_template('admin_site_form.html', site=None, form=request.form, org_units=org_units)

        site = Site(name=name, google_loaner_autodisable_enabled=bool(request.form.get('google_loaner_autodisable_enabled')),
                    loaner_org_unit_path=request.form.get('loaner_org_unit_path', '').strip() or None)
        db.session.add(site)
        db.session.flush()  # assigns site.id, used as the logo filename prefix below

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), f'site{site.id}')
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
            return render_template('admin_site_form.html', site=None, form=request.form, org_units=org_units)
        if new_logo:
            site.logo_filename = new_logo

        _log_activity('site_add', f'Added site "{name}".')
        db.session.commit()
        flash(f'Added site "{name}".', 'success')
        return redirect(url_for('admin_sites'))

    return render_template('admin_site_form.html', site=None, form=None, org_units=org_units)


@app.route('/admin/sites/<int:site_id>/edit', methods=['GET', 'POST'])
@require_super_admin
def admin_site_edit(site_id):
    site = Site.query.get_or_404(site_id)
    org_units = GoogleOrgUnit.query.order_by(GoogleOrgUnit.org_unit_path).all()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Site name is required.', 'error')
            return render_template('admin_site_form.html', site=site, form=request.form, org_units=org_units)
        dupe = Site.query.filter(db.func.lower(Site.name) == name.lower(), Site.id != site_id).first()
        if dupe:
            flash(f'A site named "{name}" already exists.', 'error')
            return render_template('admin_site_form.html', site=site, form=request.form, org_units=org_units)

        try:
            new_logo = _save_branding_logo(request.files.get('logo'), f'site{site.id}')
        except ValueError as e:
            flash(str(e), 'error')
            return render_template('admin_site_form.html', site=site, form=request.form, org_units=org_units)

        site.name = name
        site.google_loaner_autodisable_enabled = bool(request.form.get('google_loaner_autodisable_enabled'))
        site.loaner_org_unit_path = request.form.get('loaner_org_unit_path', '').strip() or None
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

    return render_template('admin_site_form.html', site=site, form=None, org_units=org_units)


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
    search = request.args.get('q', '').strip()
    if search:
        needle = search.lower()
        overdue = [r for r in overdue if needle in r.asset_tag.lower() or needle in (r.person_name or '').lower()]
    today = datetime.utcnow().date()
    return render_template('admin_reminders.html', overdue=overdue, today=today,
                           email_enabled=EMAIL_ENABLED, search=search)


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
        subject, body = _render_email_template('assignment_overdue', {
            'first_name': person.first_name, 'full_name': person.full_name, 'asset_tag': row.asset_tag,
            'due_date': row.due_date.strftime('%Y-%m-%d'),
            'days_overdue': str(days_overdue), 'days_overdue_plural': 's' if days_overdue != 1 else '',
        })
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
    if not row.is_loaner:
        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if asset and asset.assigned_to_id:
            flash(f'{asset_tag} is currently assigned to {asset.assigned_to.full_name} — unassign it first before marking it a loaner.', 'error')
            return redirect(request.referrer or url_for('admin_loaners'))
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


def _loaner_reminder_email_content(row, person, now):
    """Builds the subject/body for a loaner reminder email — shared by the
    automatic hourly overdue sweep and the admin's manual Email Selected
    action on /admin/loaners. Unlike the automatic sweep (which only ever
    sees overdue rows), the manual action can target a loaner that isn't
    due yet, so this branches on whether due_date has actually passed to
    pick which customizable template kind (see EMAIL_TEMPLATE_KINDS) applies."""
    variables = {
        'first_name': person.first_name, 'full_name': person.full_name, 'asset_tag': row.asset_tag,
        'due_date': row.due_date.strftime('%Y-%m-%d') if row.due_date else '',
        'days_overdue': '', 'days_overdue_plural': '',
    }
    if row.due_date and row.due_date < now.date():
        days_overdue = (now.date() - row.due_date).days
        variables['days_overdue'] = str(days_overdue)
        variables['days_overdue_plural'] = 's' if days_overdue != 1 else ''
        kind = 'loaner_overdue'
    elif row.due_date:
        kind = 'loaner_upcoming'
    else:
        kind = 'loaner_nodate'
    return _render_email_template(kind, variables)


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
        subject, body = _loaner_reminder_email_content(row, person, now)
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


LOANER_POOL_SORT_COLUMNS = {
    'asset_tag': (AssetRegistry.asset_tag,),
    'serial_number': (AssetRegistry.serial_number,),
    'description': (AssetRegistry.description,),
    'site': (Site.name,),
    'due_date': (LoanerCheckout.due_date,),
}


@app.route('/admin/loaners')
@require_permission('loaners')
def admin_loaners():
    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'asset_tag').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in LOANER_POOL_SORT_COLUMNS and sort != 'status':
        sort = 'asset_tag'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    today = datetime.utcnow().date()

    query = _scope_registry(AssetRegistry.query, site_ids).filter_by(is_loaner=True)
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(
            AssetRegistry.asset_tag.ilike(like), AssetRegistry.serial_number.ilike(like),
            AssetRegistry.description.ilike(like),
        ))
    if sort == 'site':
        query = query.outerjoin(Site, AssetRegistry.site_id == Site.id)
    elif sort in ('due_date', 'status'):
        # Status/due-date live on the open LoanerCheckout, not AssetRegistry
        # itself, so those two sorts need the checkout joined in first.
        query = query.outerjoin(LoanerCheckout, db.and_(
            LoanerCheckout.asset_tag == AssetRegistry.asset_tag, LoanerCheckout.checked_in_at.is_(None)))

    if sort == 'status':
        # Available (0) < Checked Out, not yet due (1) < Overdue (2).
        status_rank = db.case(
            (LoanerCheckout.id.is_(None), 0),
            (LoanerCheckout.due_date < today, 2),
            else_=1,
        )
        order_exprs = [status_rank.desc() if sort_dir == 'desc' else status_rank.asc()]
    else:
        sort_cols = LOANER_POOL_SORT_COLUMNS[sort]
        order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]
    loaner_rows = query.order_by(*order_exprs, AssetRegistry.asset_tag).all()

    tags = [r.asset_tag for r in loaner_rows]
    open_checkouts = {
        c.asset_tag: c for c in LoanerCheckout.query.filter(
            LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.is_(None)
        )
    }
    overdue_count = len(_overdue_loaners(site_ids))
    return render_template('admin_loaners.html', loaner_rows=loaner_rows, open_checkouts=open_checkouts,
                           overdue_count=overdue_count, email_enabled=EMAIL_ENABLED,
                           today=datetime.utcnow().date(), search=search, sort=sort, sort_dir=sort_dir)


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
    search = request.args.get('q', '').strip()

    assigned_rows = []
    if _has_permission('devices'):
        registry_rows = _filter_registry_by_status(
            _scope_registry(AssetRegistry.query, site_ids), 'assigned'
        ).order_by(AssetRegistry.asset_tag).all()
        tags = [r.asset_tag for r in registry_rows]
        assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(tags))}
        assigned_rows = [(r, assets_by_tag.get(r.asset_tag)) for r in registry_rows]
        if search:
            needle = search.lower()
            assigned_rows = [
                (r, a) for r, a in assigned_rows
                if needle in (r.asset_tag or '').lower() or needle in (r.description or '').lower()
                or (a and a.assigned_to and needle in a.assigned_to.full_name.lower())
            ]

    open_loaners = []
    if _has_permission('loaners'):
        loaner_query = LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None))
        if site_ids is not None:
            loaner_query = loaner_query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
                .filter(AssetRegistry.site_id.in_(site_ids))
        if search:
            like = f'%{search}%'
            loaner_query = loaner_query.filter(db.or_(
                LoanerCheckout.asset_tag.ilike(like), LoanerCheckout.person_name.ilike(like),
            ))
        open_loaners = loaner_query.order_by(LoanerCheckout.checked_out_at).all()

    return render_template('admin_collection.html', assigned_rows=assigned_rows, open_loaners=open_loaners,
                           now=datetime.utcnow().date(), search=search)


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


@app.route('/admin/loaners/email_selected', methods=['POST'])
@require_permission('loaners')
def admin_loaners_email_selected():
    """Emails a hand-picked set of currently-checked-out loaners, whether
    overdue or not — unlike admin_loaners_send_reminders (which blankets
    every overdue loaner), this lets the office remind someone their loaner
    is coming due soon, not just chase people who are already late. Ignores
    the reminder_sent_at resend gate since this is an explicit one-off send,
    not the automatic hourly sweep."""
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_FROM_EMAIL (and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth) in .env to enable it.', 'info')
        return redirect(url_for('admin_loaners'))
    site_ids = _current_site_ids()
    asset_tags = request.form.getlist('asset_tags')
    if not asset_tags:
        flash('Select at least one checked-out loaner to email.', 'error')
        return redirect(url_for('admin_loaners'))

    query = LoanerCheckout.query.filter(
        LoanerCheckout.asset_tag.in_(asset_tags), LoanerCheckout.checked_in_at.is_(None))
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    rows = query.all()

    now = datetime.utcnow()
    sent = failed = skipped = 0
    for row in rows:
        person = Person.query.get(row.person_id) if row.person_id else None
        if not person:
            skipped += 1
            continue
        subject, body = _loaner_reminder_email_content(row, person, now)
        try:
            send_email(person.email, subject, body)
            row.reminder_sent_at = now
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Loaner reminder email failed for %s -> %s: %s', row.asset_tag, person.email, e)

    if sent:
        _log_activity('reminders_send', f'Manually emailed {sent} selected loaner(s).')
    db.session.commit()
    msg = f'Emailed {sent} selected loaner{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'info')
    return redirect(url_for('admin_loaners'))


def _checkout_loaner(asset_tag, person, due_date=None, site_ids=None, acknowledged_by=None, repair_id=None):
    """Shared checkout logic used by both the admin page and student self-service.

    repair_id links this checkout to an open Repair (see admin_repair_assign_loaner)
    — someone's own device is out for repair and this loaner covers them in the
    meantime. A repair-linked checkout deliberately gets NO default due date
    (a repair's turnaround isn't a fixed N-day loan like a normal checkout), so
    it never shows up as "overdue" just because repair is taking a while.

    repair_id MUST stay the last parameter — callers invoke this positionally,
    so inserting a new param earlier would silently shift site_ids into the
    wrong argument with no error."""
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag, is_loaner=True).first()
    if not row:
        return 'error', f'{asset_tag} is not a loaner device.'
    if site_ids is not None and row.site_id not in site_ids:
        return 'error', f'{asset_tag} is not one of your site\'s loaners.'
    already_out = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    if already_out:
        return 'error', f'{asset_tag} is already checked out to {already_out.person_name}.'
    resolved_due_date = due_date
    if resolved_due_date is None and not repair_id:
        resolved_due_date = datetime.utcnow().date() + timedelta(days=LOANER_DEFAULT_LOAN_DAYS)
    db.session.add(LoanerCheckout(
        asset_tag=asset_tag, person_id=person.id, person_name=person.full_name,
        due_date=resolved_due_date, acknowledged_by=acknowledged_by, repair_id=repair_id,
    ))
    log_message = f'Checked out loaner {asset_tag} to {person.full_name}.' if not repair_id \
        else f'Checked out repair loaner {asset_tag} to {person.full_name}.'
    _log_activity('loaner_checkout', log_message, site_id=row.site_id)
    db.session.commit()
    _sync_device_google_state(row, enabled=True, person=person)
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
        _sync_device_google_state(registry_row, enabled=False)
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


@app.route('/api/loaner_lookup')
@kiosk_or_api_permission_required('loaner_checkinout')
def api_loaner_lookup():
    """
    Backs the merged /loaner_checkinout page's scan-and-detect flow: given a
    scanned tag/serial, tells the client whether this will be a checkout
    (device available) or a checkin (device currently out, and to whom) —
    without the client needing to know the difference up front. Site-scoped
    the same way the checkout/checkin routes themselves are, so a site-scoped
    kiosk/user can't learn another site's loaner status through this endpoint.
    """
    scan_value = request.args.get('scan_value', '').strip()
    if not scan_value:
        return jsonify({'found': False})
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        return jsonify({'found': False})
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    site_ids = _current_site_ids()
    if not row or not row.is_loaner or (site_ids is not None and row.site_id not in site_ids):
        return jsonify({'found': True, 'asset_tag': asset_tag, 'is_loaner': False})
    open_checkout = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    return jsonify({
        'found': True, 'asset_tag': asset_tag, 'is_loaner': True,
        'checked_out': bool(open_checkout),
        'checked_out_to': open_checkout.person_name if open_checkout else None,
    })


@app.route('/loaner_checkinout', methods=['GET', 'POST'])
@kiosk_or_permission_required('loaner_checkinout')
def loaner_checkinout_page():
    """
    Merged self-service loaner page — one scan decides checkout vs checkin
    (see api_loaner_lookup for the client-side detection), so the "average
    user" doesn't have to know or choose which of two pages they need.
    Re-derives the mode itself from current DB state rather than trusting
    anything the client sent, same as every other mutating route here.
    """
    site_ids = _current_site_ids()
    if request.method == 'POST':
        scan_value = request.form.get('scan_value', '').strip()
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkinout_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value, same as loaner_checkin_page

        open_checkout = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
        if open_checkout:
            status, message = _checkin_loaner(asset_tag, site_ids=site_ids)
        else:
            person_id = request.form.get('person_id', '').strip()
            acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
            person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
            if not person or not person.is_active:
                flash('Search for your name and select yourself from the list first.', 'error')
                return redirect(url_for('loaner_checkinout_page'))
            if not acknowledged_by:
                flash('Type your name to acknowledge responsibility for this device.', 'error')
                return redirect(url_for('loaner_checkinout_page'))
            status, message = _checkout_loaner(asset_tag, person, site_ids=site_ids, acknowledged_by=acknowledged_by)
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkinout_page'))

    return render_template('loaner_checkinout.html')


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
    """Sum of fee_amount across this person's charged-but-unpaid incidents AND
    ticket charges. Used to warn (not block) on delete/graduate."""
    incident_total = db.session.query(db.func.coalesce(db.func.sum(Incident.fee_amount), 0)).filter(
        Incident.person_id == person_id, Incident.fee_charged.is_(True), Incident.paid_at.is_(None),
    ).scalar()
    charge_total = db.session.query(db.func.coalesce(db.func.sum(TicketCharge.amount), 0)) \
        .join(Ticket, Ticket.id == TicketCharge.ticket_id) \
        .filter(Ticket.requester_person_id == person_id, TicketCharge.paid_at.is_(None)).scalar()
    return Decimal(incident_total) + Decimal(charge_total)


def _create_incident(asset_tag, person, description, fee_charged=False, fee_amount=None, repair_category_id=None):
    """Shared incident-logging logic used by both the admin page and the
    student self-service Report a Problem page. Does not commit — caller's
    responsibility, matching _assign_asset_to_person/_checkout_loaner."""
    incident = Incident(
        asset_tag=asset_tag, person_id=person.id if person else None,
        person_name=person.full_name if person else None,
        description=description, fee_charged=fee_charged, fee_amount=fee_amount,
        repair_category_id=repair_category_id,
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
    regardless of the checkbox — an amount implies a charge. Picking a repair
    category is optional (the form JS pre-fills description/fee_amount from
    it, but both stay freely editable, so this route just trusts whatever the
    form actually submitted)."""
    _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    description = request.form.get('description', '').strip()
    fee_charged = request.form.get('fee_charged') == 'on'
    fee_amount = _parse_money(request.form.get('fee_amount'))
    repair_category_id = request.form.get('repair_category_id', type=int)
    if fee_amount:
        fee_charged = True
    if not description:
        flash('Enter a description of the incident.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None
    try:
        _create_incident(asset_tag, person, description, fee_charged=fee_charged, fee_amount=fee_amount,
                          repair_category_id=repair_category_id)
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


@app.route('/admin/incidents/<int:incident_id>/delete', methods=['POST'])
@require_permission('devices')
def admin_incident_delete(incident_id):
    """Permanently removes an incident (and its fee, if any) — for
    correcting billing mistakes like a duplicate entry or a charge logged
    against the wrong person. Unlike editing the fee to blank, this drops
    the row entirely, so it also stops counting toward that device's
    incident-escalation history."""
    incident = Incident.query.get_or_404(incident_id)
    asset_tag, person_name, description = incident.asset_tag, incident.person_name, incident.description
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    db.session.delete(incident)
    _log_activity('incident_delete', f'Deleted incident on {asset_tag} ({person_name or "unknown"}): {description}',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    flash('Incident deleted.', 'success')
    return redirect(request.referrer or url_for('admin_fees'))


@app.route('/admin/fees')
@require_permission('devices')
def admin_fees():
    """Centralized billing — charged incidents AND ticket fees, merged and
    grouped by person, with inline edit/delete/mark-paid on every line so
    the office never has to hunt down the originating device or ticket to
    fix a mistake. Ticket fees are only folded in when the viewer also
    holds the 'tickets' permission — otherwise the links on those rows
    would 403 for them. status defaults to 'unpaid' (the original "who
    owes" view); 'paid'/'all' add billing history."""
    site_ids = _current_site_ids()
    status = request.args.get('status', 'unpaid').strip()
    if status not in ('unpaid', 'paid', 'all'):
        status = 'unpaid'
    search = request.args.get('q', '').strip()

    inc_query = Incident.query.filter(Incident.fee_charged.is_(True))
    if status == 'unpaid':
        inc_query = inc_query.filter(Incident.paid_at.is_(None))
    elif status == 'paid':
        inc_query = inc_query.filter(Incident.paid_at.isnot(None))
    if site_ids is not None:
        inc_query = inc_query.join(AssetRegistry, AssetRegistry.asset_tag == Incident.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    if search:
        like = f'%{search}%'
        inc_query = inc_query.filter(db.or_(
            Incident.person_name.ilike(like), Incident.description.ilike(like), Incident.asset_tag.ilike(like)))
    incidents = inc_query.order_by(Incident.person_name, Incident.created_at).all()

    by_person = OrderedDict()
    grand_total = Decimal('0')
    for inc in incidents:
        key = inc.person_name or '(no person on file)'
        entry = by_person.setdefault(key, {'charges': [], 'subtotal': Decimal('0')})
        amount = inc.fee_amount or Decimal('0')
        entry['charges'].append({
            'date': inc.created_at, 'label': f'Incident: {inc.description}', 'amount': amount,
            'paid': inc.paid_at is not None, 'is_ticket_charge': False,
            'mark_paid_url': url_for('admin_incident_mark_paid', incident_id=inc.id),
            'fee_url': url_for('admin_incident_fee', incident_id=inc.id),
            'delete_url': url_for('admin_incident_delete', incident_id=inc.id),
            'delete_confirm': f'Delete this incident ({inc.asset_tag}, ${amount:.2f})? This removes the incident '
                               'record entirely (not just the charge) — it will no longer count toward that '
                               'device\'s incident history.',
            'link_url': url_for('admin_asset_assign', asset_tag=inc.asset_tag), 'link_label': inc.asset_tag,
        })
        entry['subtotal'] += amount
        grand_total += amount

    if _has_permission('tickets'):
        tc_query = _scope_tickets(TicketCharge.query.join(Ticket, Ticket.id == TicketCharge.ticket_id), site_ids)
        if status == 'unpaid':
            tc_query = tc_query.filter(TicketCharge.paid_at.is_(None))
        elif status == 'paid':
            tc_query = tc_query.filter(TicketCharge.paid_at.isnot(None))
        if search:
            like = f'%{search}%'
            tc_query = tc_query.filter(db.or_(
                Ticket.requester_name.ilike(like), Ticket.subject.ilike(like), TicketCharge.description.ilike(like)))
        charges = tc_query.order_by(Ticket.requester_name, TicketCharge.created_at).all()
        for tc in charges:
            t = tc.ticket
            key = t.requester_name or '(no person on file)'
            entry = by_person.setdefault(key, {'charges': [], 'subtotal': Decimal('0')})
            amount = tc.amount or Decimal('0')
            entry['charges'].append({
                'date': tc.created_at, 'label': f'Ticket #{t.id}: {tc.description}', 'amount': amount,
                'paid': tc.paid_at is not None, 'is_ticket_charge': True, 'description': tc.description,
                'mark_paid_url': url_for('admin_ticket_charge_mark_paid', charge_id=tc.id),
                'fee_url': url_for('admin_ticket_charge_edit', charge_id=tc.id),
                'delete_url': url_for('admin_ticket_charge_delete', charge_id=tc.id),
                'delete_confirm': f'Delete this ${amount:.2f} charge from ticket #{t.id}? This cannot be undone.',
                'link_url': url_for('admin_ticket_detail', ticket_id=t.id), 'link_label': f'#{t.id}',
            })
            entry['subtotal'] += amount
            grand_total += amount

    for entry in by_person.values():
        entry['charges'].sort(key=lambda c: c['date'])
    by_person = OrderedDict(sorted(by_person.items(), key=lambda kv: kv[0]))

    return render_template('admin_fees.html', by_person=by_person, grand_total=grand_total, status=status)


@app.route('/admin/assets/<string:asset_tag>/invoice')
@require_permission('devices')
def admin_asset_invoice(asset_tag):
    """Printable invoice listing every incident logged against this device —
    the "list of damages" comes from however many separate Incident rows
    exist for this asset_tag, each optionally tagged with a RepairCategory;
    no separate line-item table needed since Incident is already a per-asset
    list. Browser print (window.print()), same as any other print-friendly
    page in this app — no PDF library involved."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    incidents = Incident.query.filter_by(asset_tag=asset_tag).order_by(Incident.created_at).all()
    total = sum((inc.fee_amount or Decimal('0')) for inc in incidents if inc.fee_charged)
    return render_template('admin_asset_invoice.html', registry_row=registry_row, incidents=incidents,
                           total=total, branding_settings=BrandingSettings.query.get(1),
                           generated_at=datetime.utcnow())


@app.route('/admin/repair_categories')
@require_permission('devices')
def admin_repair_categories():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = RepairCategory.query
    if search:
        query = query.filter(RepairCategory.name.ilike(f'%{search}%'))
    categories = query.order_by(RepairCategory.name.desc() if sort_dir == 'desc' else RepairCategory.name.asc()).all()
    return render_template('admin_repair_categories.html', categories=categories, search=search, sort_dir=sort_dir)


@app.route('/admin/repair_categories/new', methods=['GET', 'POST'])
@require_permission('devices')
def admin_repair_category_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_repair_category_form.html', category=None, form=request.form)
        if RepairCategory.query.filter(db.func.lower(RepairCategory.name) == name.lower()).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_repair_category_form.html', category=None, form=request.form)

        db.session.add(RepairCategory(name=name, default_price=default_price))
        _log_activity('repair_category_add', f'Added repair category "{name}".')
        db.session.commit()
        flash(f'Added category "{name}".', 'success')
        return redirect(url_for('admin_repair_categories'))

    return render_template('admin_repair_category_form.html', category=None, form=None)


@app.route('/admin/repair_categories/<int:category_id>/edit', methods=['GET', 'POST'])
@require_permission('devices')
def admin_repair_category_edit(category_id):
    category = RepairCategory.query.get_or_404(category_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        is_active = bool(request.form.get('is_active'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_repair_category_form.html', category=category, form=None)
        if RepairCategory.query.filter(db.func.lower(RepairCategory.name) == name.lower(),
                                        RepairCategory.id != category_id).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_repair_category_form.html', category=category, form=None)

        category.name = name
        category.default_price = default_price
        category.is_active = is_active
        _log_activity('repair_category_edit', f'Edited repair category "{name}".')
        db.session.commit()
        flash(f'Updated category "{name}".', 'success')
        return redirect(url_for('admin_repair_categories'))

    return render_template('admin_repair_category_form.html', category=category, form=None)


@app.route('/admin/repair_categories/<int:category_id>/delete', methods=['POST'])
@require_permission('devices')
def admin_repair_category_delete(category_id):
    category = RepairCategory.query.get_or_404(category_id)
    in_use = Incident.query.filter_by(repair_category_id=category_id).count()
    if in_use:
        flash(f'Cannot delete "{category.name}" — {in_use} incident(s) still reference it. Deactivate it instead.', 'error')
        return redirect(url_for('admin_repair_categories'))
    name = category.name
    db.session.delete(category)
    _log_activity('repair_category_delete', f'Deleted repair category "{name}".')
    db.session.commit()
    flash(f'Deleted category "{name}".', 'success')
    return redirect(url_for('admin_repair_categories'))


# ─── Repairs ──────────────────────────────────────────────────────────────────

def _scope_repairs(query, site_ids):
    """Repair has no site_id of its own — scope via a join through AssetRegistry,
    same pattern _overdue_assignments/_overdue_loaners use."""
    if site_ids is None:
        return query
    return query.join(AssetRegistry, AssetRegistry.asset_tag == Repair.asset_tag) \
        .filter(AssetRegistry.site_id.in_(site_ids))


def _get_or_create_repair_ticket_category():
    """Ensures a 'Device Repair' ticket category exists to file auto-opened
    repair tickets under, so a repair shows up in the normal help-desk queue
    instead of living invisibly outside it."""
    category = TicketCategory.query.filter_by(name='Device Repair').first()
    if not category:
        category = TicketCategory(name='Device Repair', is_active=True)
        db.session.add(category)
        db.session.flush()  # assigns category.id for the ticket we're about to create
    return category


def _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description, expected_return_at, site_ids=None):
    """Shared repair-creation logic used by both the device-page form and the
    quick Send to Repair flow on /admin/repairs — creates the tracking
    record, opens a matching help-desk Ticket (so repairs show up in the
    normal ticket queue/history, not just the Repairs page), and sets the
    asset status to 'repair', all together. Does not commit; returns
    (status, message) same as the _checkout_loaner/_checkin_loaner pattern.
    issue_description is required (checked here so both callers get the same
    validation for free); repair_category_id is optional, same reasoning as
    Incident's repair_category_id."""
    issue_description = (issue_description or '').strip()
    if not issue_description:
        return 'error', 'Describe the issue before sending a device to repair.'
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first()
    if not registry_row:
        return 'error', f'"{asset_tag}" was not found in the asset registry.'
    if Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first():
        return 'error', f'{asset_tag} already has an open repair.'

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None

    repair_category = RepairCategory.query.get(repair_category_id) if repair_category_id else None
    ticket_category = _get_or_create_repair_ticket_category()
    subject = f'Repair: {asset_tag}' + (f' ({repair_category.name})' if repair_category else '')
    ticket = _create_ticket(
        ticket_category.id, subject, issue_description,
        person=person, asset_tag=asset_tag, site_id=registry_row.site_id,
    )

    db.session.add(Repair(
        asset_tag=asset_tag, repair_category_id=repair_category.id if repair_category else None,
        ticket_number=ticket_number, issue_description=issue_description, expected_return_at=expected_return_at,
        person_name_snapshot=person.full_name if person else None, ticket_id=ticket.id,
    ))
    if not asset:
        asset = Asset(asset_tag=asset_tag, is_valid=True)
        db.session.add(asset)
    asset.status = 'repair'
    _log_activity('repair_send', f'Sent {asset_tag} to repair{" (" + repair_category.name + ")" if repair_category else ""}.',
                   site_id=registry_row.site_id)
    return 'ok', f'{asset_tag} sent to repair — opened ticket #{ticket.id}.'


@app.route('/admin/assets/<string:asset_tag>/repairs/send', methods=['POST'])
@require_permission('repairs')
def admin_repair_send(asset_tag):
    repair_category_id = request.form.get('repair_category_id', type=int)
    ticket_number = request.form.get('ticket_number', '').strip() or None
    issue_description = request.form.get('issue_description', '').strip() or None
    expected_return_at = _parse_date(request.form.get('expected_return_at'))
    try:
        status, message = _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description,
                                                   expected_return_at, site_ids=_current_site_ids())
        (db.session.commit if status == 'ok' else db.session.rollback)()
        flash(message, 'success' if status == 'ok' else 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not send device to repair: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/repairs/send', methods=['POST'])
@require_permission('repairs')
def admin_repair_send_quick():
    """Quick-start version for the Repairs page — takes a raw scan value
    (tag or serial) instead of requiring you to already be on the device's
    own page, same shape as the merged Loaner Check In/Out flow."""
    scan_value = request.form.get('scan_value', '').strip()
    if not scan_value:
        flash('Scan or type the device asset tag/serial.', 'error')
        return redirect(url_for('admin_repairs'))
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        flash(f'"{scan_value}" was not found in the asset registry.', 'error')
        return redirect(url_for('admin_repairs'))

    repair_category_id = request.form.get('repair_category_id', type=int)
    ticket_number = request.form.get('ticket_number', '').strip() or None
    issue_description = request.form.get('issue_description', '').strip() or None
    expected_return_at = _parse_date(request.form.get('expected_return_at'))
    try:
        status, message = _send_device_to_repair(asset_tag, repair_category_id, ticket_number, issue_description,
                                                   expected_return_at, site_ids=_current_site_ids())
        (db.session.commit if status == 'ok' else db.session.rollback)()
        flash(message, 'success' if status == 'ok' else 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not send device to repair: {e}', 'error')
    return redirect(url_for('admin_repairs'))


@app.route('/admin/repairs/<int:repair_id>')
@require_permission('repairs')
def admin_repair_detail(repair_id):
    """A repair's own page — details, edit, loaner assignment, and billing
    (via the linked Ticket's charges) all together, so billing a repair
    doesn't require jumping over to the generic Ticket detail page. The
    Ticket itself (status/priority/assignment/comments) stays one click
    away via the 'View full ticket' link, for the cases that still need it."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    active_loaner = LoanerCheckout.query.filter_by(repair_id=repair.id, checked_in_at=None).first()
    return render_template('admin_repair_detail.html', repair=repair, repair_categories=repair_categories,
                           repair_outcomes=REPAIR_OUTCOMES, active_loaner=active_loaner)


@app.route('/admin/repairs/<int:repair_id>/return', methods=['POST'])
@require_permission('repairs')
def admin_repair_return(repair_id):
    """Marks an open repair returned. The outcome drives what happens to the
    device's status next: Fixed goes back into service, Could Not Repair and
    Replaced both retire this physical unit (a Replaced device's replacement
    is added separately as an ordinary new device, not automated here).
    Redirects back to wherever the form was submitted from (device page or
    the Repairs list), so it works from both entry points."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_repair_detail', repair_id=repair.id)
    outcome = request.form.get('outcome', '')
    if outcome not in REPAIR_OUTCOMES:
        flash('Choose a valid outcome.', 'error')
        return redirect(request.referrer or fallback_url)

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
        if repair.ticket_id:
            _, actor_label, _ = _current_actor()
            comment_body = f'Returned from repair: {REPAIR_OUTCOMES[outcome]}.' + (f' {notes}' if notes else '')
            db.session.add(TicketComment(ticket_id=repair.ticket_id, body=comment_body, author_label=actor_label))
            repair.ticket.updated_at = datetime.utcnow()
        _log_activity('repair_return', f'{repair.asset_tag} returned from repair ({REPAIR_OUTCOMES[outcome]}).',
                       site_id=registry_row.site_id if registry_row else None, ticket_id=repair.ticket_id)
        open_loaner = LoanerCheckout.query.filter_by(repair_id=repair.id, checked_in_at=None).first()
        db.session.commit()
        msg = f'{repair.asset_tag} marked returned ({REPAIR_OUTCOMES[outcome]}).'
        if open_loaner:
            msg += f' Note: loaner {open_loaner.asset_tag} is still checked out to {open_loaner.person_name} — check it in once it\'s back.'
        flash(msg, 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update repair: {e}', 'error')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/repairs/<int:repair_id>/edit', methods=['POST'])
@require_permission('repairs')
def admin_repair_edit(repair_id):
    """Corrects an open repair's tracking details after the fact (wrong
    category, wrong RMA ticket #, an updated expected-return date) without
    having to close it out and re-send. Only open repairs are editable —
    a closed repair's record is historical."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_repair_detail', repair_id=repair.id)
    if repair.returned_at:
        flash('This repair has already been closed out and can no longer be edited.', 'error')
        return redirect(request.referrer or fallback_url)

    issue_description = request.form.get('issue_description', '').strip()
    if not issue_description:
        flash('Describe the issue — this field is required.', 'error')
        return redirect(request.referrer or fallback_url)

    repair.repair_category_id = request.form.get('repair_category_id', type=int) or None
    repair.ticket_number = request.form.get('ticket_number', '').strip() or None
    repair.issue_description = issue_description
    repair.expected_return_at = _parse_date(request.form.get('expected_return_at'))
    registry_row = AssetRegistry.query.filter_by(asset_tag=repair.asset_tag).first()
    _log_activity('repair_edit', f'Updated repair details for {repair.asset_tag}.',
                   site_id=registry_row.site_id if registry_row else None, ticket_id=repair.ticket_id)
    db.session.commit()
    flash('Repair updated.', 'success')
    return redirect(request.referrer or fallback_url)


@app.route('/admin/repairs/<int:repair_id>/assign_loaner', methods=['POST'])
@require_permission('repairs')
def admin_repair_assign_loaner(repair_id):
    """Checks out a loaner (from the loaner pool) to cover someone whose own
    device is at repair.asset_tag while it's out for repair — a thin wrapper
    over the same _checkout_loaner() the main Loaners page uses, just with
    repair_id set so it's tracked back to this repair and shows up flagged
    on the loaner pool (see admin_loaners())."""
    site_ids = _current_site_ids()
    repair = _scope_repairs(Repair.query, site_ids).filter(Repair.id == repair_id).first_or_404()
    fallback_url = url_for('admin_ticket_detail', ticket_id=repair.ticket_id) if repair.ticket_id \
        else url_for('admin_repairs')
    if repair.returned_at:
        flash('This repair has already been closed out.', 'error')
        return redirect(request.referrer or fallback_url)
    if LoanerCheckout.query.filter_by(repair_id=repair.id, checked_in_at=None).first():
        flash('This repair already has a loaner checked out.', 'error')
        return redirect(request.referrer or fallback_url)

    person_id = request.form.get('person_id', '').strip()
    scan_value = request.form.get('loaner_asset_tag', '').strip()
    due_date = _parse_date(request.form.get('due_date'))
    person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
    if not person or not scan_value:
        flash('Choose both a person and a loaner asset tag.', 'error')
        return redirect(request.referrer or fallback_url)
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        flash(f'"{scan_value}" was not found in the asset registry.', 'error')
        return redirect(request.referrer or fallback_url)

    status, message = _checkout_loaner(asset_tag, person, due_date, site_ids, repair_id=repair.id)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(request.referrer or fallback_url)


REPAIR_SORT_COLUMNS = {
    'asset_tag': (Repair.asset_tag,),
    'category': (RepairCategory.name,),
    'ticket_number': (Repair.ticket_number,),
    'sent_at': (Repair.sent_at,),
    'expected_return_at': (Repair.expected_return_at,),
    'person': (Repair.person_name_snapshot,),
}


@app.route('/admin/repairs')
@require_permission('repairs')
def admin_repairs():
    """Fleet-wide open + recent-closed repair list."""
    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'sent_at').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort not in REPAIR_SORT_COLUMNS:
        sort = 'sent_at'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'
    sort_cols = REPAIR_SORT_COLUMNS[sort]
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]

    def _apply_search(query):
        query = query.outerjoin(RepairCategory, Repair.repair_category_id == RepairCategory.id)
        if not search:
            return query
        like = f'%{search}%'
        return query.filter(db.or_(
            Repair.asset_tag.ilike(like), RepairCategory.name.ilike(like), Repair.ticket_number.ilike(like),
            Repair.issue_description.ilike(like), Repair.person_name_snapshot.ilike(like),
        ))

    open_repairs = _apply_search(_scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.is_(None))) \
        .order_by(*order_exprs).all()
    closed_repairs = _apply_search(_scope_repairs(Repair.query, site_ids).filter(Repair.returned_at.isnot(None))) \
        .order_by(Repair.returned_at.desc()).limit(50).all()
    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    open_repair_ids = [r.id for r in open_repairs]
    active_repair_loaners = {
        c.repair_id: c for c in LoanerCheckout.query.filter(
            LoanerCheckout.repair_id.in_(open_repair_ids), LoanerCheckout.checked_in_at.is_(None))
    } if open_repair_ids else {}
    return render_template('admin_repairs.html', open_repairs=open_repairs, closed_repairs=closed_repairs,
                           repair_outcomes=REPAIR_OUTCOMES, repair_categories=repair_categories,
                           active_repair_loaners=active_repair_loaners,
                           today=datetime.utcnow().date(), search=search, sort=sort, sort_dir=sort_dir)


# ─── Tickets ───────────────────────────────────────────────────────────────────
# General IT help-desk requests — unlike Incident (damage/fee) or Repair (RMA),
# a ticket doesn't have to be tied to a specific asset.

def _scope_tickets(query, site_ids):
    """Ticket carries its own site_id directly (no join needed, unlike Repair)."""
    if site_ids is None:
        return query
    return query.filter(Ticket.site_id.in_(site_ids))


def _ticket_assignees(site_ids):
    """Users who can be assigned a ticket: active, with can_tickets or is_admin,
    scoped to the current actor's sites same as everything else site-scoped."""
    return _scope_users(User.query, site_ids).filter(
        User.is_active.is_(True), db.or_(User.can_tickets.is_(True), User.is_admin.is_(True)),
    ).order_by(User.username).all()


def _create_ticket(category_id, subject, description, person=None, requester_name=None,
                    requester_email=None, asset_tag=None, site_id=None, priority='normal'):
    """Shared ticket-creation logic used by both the public submission page and
    the admin-initiated New Ticket form. Does not commit — caller's responsibility.
    A category with a default_price seeds one initial itemized TicketCharge at
    creation time (correctable/removable later, more can be added as the
    ticket progresses) — no charge fields shown on either creation form,
    matching report_problem_page's "not the submitter's decision" reasoning."""
    category = TicketCategory.query.get(category_id)
    default_price = category.default_price if category else None
    ticket = Ticket(
        category_id=category_id, subject=subject, description=description,
        priority=priority if priority in TICKET_PRIORITIES else 'normal',
        site_id=site_id, asset_tag=asset_tag or None,
        requester_person_id=person.id if person else None,
        requester_name=person.full_name if person else (requester_name or None),
        requester_email=person.email if person else (requester_email or None),
    )
    db.session.add(ticket)
    db.session.flush()  # assigns ticket.id, needed so the log entry (and any charge) can link back to it
    if default_price:
        db.session.add(TicketCharge(ticket_id=ticket.id, description=category.name, amount=default_price))
    _log_activity('ticket_add', f'Opened ticket #{ticket.id}: {subject}', site_id=site_id, ticket_id=ticket.id)
    return ticket


@app.route('/submit_ticket', methods=['GET', 'POST'])
@kiosk_or_permission_required('checkinout')
def submit_ticket_page():
    """Student/staff self-service ticket submission — mirrors report_problem_page's
    shape (person-search + optional scan), but a ticket isn't always about a
    specific device, so an unresolved/blank scan is allowed here."""
    site_ids = _current_site_ids()
    categories = TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        scan_value = request.form.get('scan_value', '').strip()

        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('submit_ticket_page'))
        if not category_id or not TicketCategory.query.filter_by(id=category_id, is_active=True).first():
            flash('Choose a category.', 'error')
            return redirect(url_for('submit_ticket_page'))
        if not subject:
            flash('Give the ticket a short subject.', 'error')
            return redirect(url_for('submit_ticket_page'))
        if not description:
            flash('Describe the issue.', 'error')
            return redirect(url_for('submit_ticket_page'))

        asset_tag = None
        if scan_value:
            asset_tag, _ = resolve_scan(scan_value)
            if not asset_tag:
                asset_tag = scan_value  # fall back to raw value, same as report_problem_page

        try:
            _create_ticket(category_id, subject, description, person=person,
                            asset_tag=asset_tag, site_id=person.site_id)
            db.session.commit()
            flash('Thanks — your ticket has been submitted.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Could not submit ticket: {e}', 'error')
        return redirect(url_for('submit_ticket_page'))

    return render_template('submit_ticket.html', categories=categories)


TICKET_SORT_COLUMNS = {
    'created_at': (Ticket.created_at,),
    'subject': (Ticket.subject,),
    'category': (TicketCategory.name,),
    'status': (Ticket.status,),
    'priority': (Ticket.priority,),
    'requester': (Ticket.requester_name,),
    'assignee': (User.username,),
}


@app.route('/admin/tickets')
@require_permission('tickets')
def admin_tickets():
    site_ids = _current_site_ids()
    page = request.args.get('page', 1, type=int)
    per_page = 50
    status_filter = request.args.get('status', '').strip()
    category_filter = request.args.get('category_id', type=int)
    assignee_filter = request.args.get('assigned_to_user_id', type=int)
    search = request.args.get('q', '').strip()

    query = _scope_tickets(Ticket.query, site_ids)
    if status_filter and status_filter in TICKET_STATUSES:
        query = query.filter(Ticket.status == status_filter)
    elif not status_filter:
        query = query.filter(Ticket.status.in_(['open', 'in_progress']))
    if category_filter:
        query = query.filter(Ticket.category_id == category_filter)
    if assignee_filter:
        query = query.filter(Ticket.assigned_to_user_id == assignee_filter)
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(
            Ticket.subject.ilike(like),
            Ticket.description.ilike(like),
            Ticket.requester_name.ilike(like),
            Ticket.asset_tag.ilike(like),
        ))

    sort = request.args.get('sort', 'created_at').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort not in TICKET_SORT_COLUMNS:
        sort = 'created_at'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'
    if sort == 'category':
        query = query.outerjoin(TicketCategory, Ticket.category_id == TicketCategory.id)
    elif sort == 'assignee':
        query = query.outerjoin(User, Ticket.assigned_to_user_id == User.id)
    sort_cols = TICKET_SORT_COLUMNS[sort]
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]
    query = query.order_by(*order_exprs, Ticket.created_at.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    categories = TicketCategory.query.order_by(TicketCategory.name).all()
    assignees = _ticket_assignees(site_ids)
    return render_template('admin_tickets.html', pagination=pagination, categories=categories, assignees=assignees,
                           statuses=TICKET_STATUSES, status_filter=status_filter, search=search,
                           category_filter=category_filter, assignee_filter=assignee_filter,
                           sort=sort, sort_dir=sort_dir)


@app.route('/admin/tickets/new', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_new():
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    categories = TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        priority = request.form.get('priority', 'normal').strip()
        asset_tag = request.form.get('asset_tag', '').strip() or None
        site_id = request.form.get('site_id', type=int)

        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None

        if not category_id or not TicketCategory.query.filter_by(id=category_id, is_active=True).first():
            flash('Choose a category.', 'error')
            return render_template('admin_ticket_form.html', categories=categories, sites=sites, form=request.form)
        if not subject:
            flash('Give the ticket a short subject.', 'error')
            return render_template('admin_ticket_form.html', categories=categories, sites=sites, form=request.form)
        if not description:
            flash('Describe the issue.', 'error')
            return render_template('admin_ticket_form.html', categories=categories, sites=sites, form=request.form)
        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_ticket_form.html', categories=categories, sites=sites, form=request.form)

        try:
            ticket = _create_ticket(category_id, subject, description, person=person,
                                     asset_tag=asset_tag, site_id=site_id, priority=priority)
            db.session.commit()
            flash('Ticket created.', 'success')
            return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not create ticket: {e}', 'error')

    return render_template('admin_ticket_form.html', categories=categories, sites=sites, form=None, priorities=TICKET_PRIORITIES)


@app.route('/admin/tickets/<int:ticket_id>')
@require_permission('tickets')
def admin_ticket_detail(ticket_id):
    site_ids = _current_site_ids()
    ticket = _scope_tickets(Ticket.query, site_ids).filter_by(id=ticket_id).first_or_404()
    registry_row = AssetRegistry.query.filter_by(asset_tag=ticket.asset_tag).first() if ticket.asset_tag else None
    history = ActivityLog.query.filter_by(ticket_id=ticket.id).order_by(ActivityLog.timestamp.desc()).all()
    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    return render_template('admin_ticket_detail.html', ticket=ticket, registry_row=registry_row,
                           statuses=TICKET_STATUSES, priorities=TICKET_PRIORITIES,
                           assignees=_ticket_assignees(site_ids), history=history,
                           repair_outcomes=REPAIR_OUTCOMES, repair_categories=repair_categories)


@app.route('/admin/tickets/<int:ticket_id>/edit', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_edit(ticket_id):
    """Edits a ticket's core fields (subject, description, category, linked
    asset, site, requester) — separate from the status/priority/assignment
    mini-forms on the detail page, which already worked fine and didn't need
    touching."""
    site_ids = _current_site_ids()
    ticket = _scope_tickets(Ticket.query, site_ids).filter_by(id=ticket_id).first_or_404()
    sites = _sites_for_actor(site_ids)
    categories = TicketCategory.query.order_by(TicketCategory.name).all()

    if request.method == 'POST':
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        asset_tag = request.form.get('asset_tag', '').strip() or None
        site_id = request.form.get('site_id', type=int)
        person_id = request.form.get('person_id', '').strip()

        if not category_id or not TicketCategory.query.get(category_id):
            flash('Choose a category.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)
        if not subject:
            flash('Give the ticket a short subject.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)
        if not description:
            flash('Describe the issue.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)
        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)

        changes = []
        if ticket.category_id != category_id:
            old_name = ticket.category.name if ticket.category else 'none'
            new_name = TicketCategory.query.get(category_id).name
            changes.append(f'category: "{old_name}" → "{new_name}"')
            ticket.category_id = category_id
        if ticket.subject != subject:
            changes.append(f'subject: "{ticket.subject}" → "{subject}"')
            ticket.subject = subject
        if ticket.description != description:
            changes.append('description updated')
            ticket.description = description
        if ticket.asset_tag != asset_tag:
            changes.append(f'device: {ticket.asset_tag or "none"} → {asset_tag or "none"}')
            ticket.asset_tag = asset_tag
        if ticket.site_id != site_id:
            changes.append('site updated')
            ticket.site_id = site_id
        if person_id.isdigit():
            person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first()
            if person and ticket.requester_person_id != person.id:
                changes.append(f'requester: "{ticket.requester_name or "none"}" → "{person.full_name}"')
                ticket.requester_person_id = person.id
                ticket.requester_name = person.full_name
                ticket.requester_email = person.email

        ticket.updated_at = datetime.utcnow()
        if changes:
            _log_activity('ticket_edit', f'Edited ticket #{ticket.id} — {"; ".join(changes)}.',
                           site_id=ticket.site_id, ticket_id=ticket.id)
        db.session.commit()
        flash(f'Ticket #{ticket.id} updated.', 'success')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))

    return render_template('admin_ticket_edit.html', ticket=ticket, categories=categories, sites=sites)


@app.route('/admin/tickets/<int:ticket_id>/comment', methods=['POST'])
@require_permission('tickets')
def admin_ticket_comment(ticket_id):
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    body = request.form.get('body', '').strip()
    if not body:
        flash('Comment cannot be blank.', 'error')
        return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))
    _, actor_label, _ = _current_actor()
    db.session.add(TicketComment(ticket_id=ticket.id, body=body, author_label=actor_label))
    ticket.updated_at = datetime.utcnow()
    _log_activity('ticket_comment', f'Commented on ticket #{ticket.id}.', site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    flash('Comment added.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/status', methods=['POST'])
@require_permission('tickets')
def admin_ticket_status(ticket_id):
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    status = request.form.get('status', '').strip()
    priority = request.form.get('priority', '').strip()
    old_status, old_priority = ticket.status, ticket.priority
    if status and status in TICKET_STATUSES:
        ticket.status = status
        ticket.resolved_at = datetime.utcnow() if status in ('resolved', 'closed') else None
    if priority and priority in TICKET_PRIORITIES:
        ticket.priority = priority
    ticket.updated_at = datetime.utcnow()
    changes = []
    if old_status != ticket.status:
        changes.append(f'status: {old_status} → {ticket.status}')
    if old_priority != ticket.priority:
        changes.append(f'priority: {old_priority} → {ticket.priority}')
    if changes:
        _log_activity('ticket_status', f'Ticket #{ticket.id} — {"; ".join(changes)}.', site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    flash(f'Ticket #{ticket.id} updated.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/assign', methods=['POST'])
@require_permission('tickets')
def admin_ticket_assign(ticket_id):
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    old_label = ticket.assigned_to.username if ticket.assigned_to_user_id and ticket.assigned_to else 'nobody'
    assignee_id = request.form.get('assigned_to_user_id', type=int)
    ticket.assigned_to_user_id = assignee_id or None
    ticket.updated_at = datetime.utcnow()
    label = ticket.assigned_to.username if ticket.assigned_to_user_id and ticket.assigned_to else 'nobody'
    _log_activity('ticket_assign', f'Ticket #{ticket.id} reassigned: {old_label} → {label}.', site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    flash(f'Ticket #{ticket.id} assigned to {label}.', 'success')
    return redirect(url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/tickets/<int:ticket_id>/charges', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_add(ticket_id):
    """Adds one itemized charge to a ticket. Tickets can carry several
    distinct charges over their life, so this can be called repeatedly."""
    ticket = _scope_tickets(Ticket.query, _current_site_ids()).filter_by(id=ticket_id).first_or_404()
    description = request.form.get('description', '').strip()
    amount = _parse_money(request.form.get('amount'))
    if not description:
        flash('Describe what this charge is for.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))
    if not amount:
        flash('Enter a charge amount.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))
    db.session.add(TicketCharge(ticket_id=ticket.id, description=description, amount=amount))
    ticket.updated_at = datetime.utcnow()
    _log_activity('ticket_charge_add', f'Added ${amount:.2f} charge to ticket #{ticket.id}: {description}',
                   site_id=ticket.site_id, ticket_id=ticket.id)
    db.session.commit()
    flash('Charge added.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/ticket_charges/<int:charge_id>/edit', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_edit(charge_id):
    """Corrects a charge's description/amount after the fact — same
    reasoning as admin_incident_fee (the estimate didn't match what the
    office actually decided to charge)."""
    charge = TicketCharge.query.get_or_404(charge_id)
    description = request.form.get('description', '').strip()
    amount = _parse_money(request.form.get('amount'))
    if not description:
        flash('Describe what this charge is for.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))
    if not amount:
        flash('Enter a charge amount.', 'error')
        return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))
    charge.description = description
    charge.amount = amount
    charge.ticket.updated_at = datetime.utcnow()
    _log_activity('fee_edit', f'Updated charge on ticket #{charge.ticket_id} to ${amount:.2f}: {description}',
                   site_id=charge.ticket.site_id, ticket_id=charge.ticket_id)
    db.session.commit()
    flash('Charge updated.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))


@app.route('/admin/ticket_charges/<int:charge_id>/delete', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_delete(charge_id):
    charge = TicketCharge.query.get_or_404(charge_id)
    ticket_id, description, amount, site_id = charge.ticket_id, charge.description, charge.amount, charge.ticket.site_id
    db.session.delete(charge)
    _log_activity('ticket_charge_delete', f'Deleted ${amount:.2f} charge from ticket #{ticket_id}: {description}',
                   site_id=site_id, ticket_id=ticket_id)
    db.session.commit()
    flash('Charge deleted.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=ticket_id))


@app.route('/admin/ticket_charges/<int:charge_id>/mark_paid', methods=['POST'])
@require_permission('tickets')
def admin_ticket_charge_mark_paid(charge_id):
    charge = TicketCharge.query.get_or_404(charge_id)
    charge.paid_at = datetime.utcnow()
    charge.ticket.updated_at = datetime.utcnow()
    _log_activity('fee_paid', f'Marked ${charge.amount:.2f} charge paid on ticket #{charge.ticket_id}.',
                   site_id=charge.ticket.site_id, ticket_id=charge.ticket_id)
    db.session.commit()
    flash('Marked paid.', 'success')
    return redirect(request.referrer or url_for('admin_ticket_detail', ticket_id=charge.ticket_id))


@app.route('/admin/ticket_categories')
@require_permission('tickets')
def admin_ticket_categories():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = TicketCategory.query
    if search:
        query = query.filter(TicketCategory.name.ilike(f'%{search}%'))
    categories = query.order_by(TicketCategory.name.desc() if sort_dir == 'desc' else TicketCategory.name.asc()).all()
    return render_template('admin_ticket_categories.html', categories=categories, search=search, sort_dir=sort_dir)


@app.route('/admin/ticket_categories/new', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_category_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_ticket_category_form.html', category=None, form=request.form)
        if TicketCategory.query.filter(db.func.lower(TicketCategory.name) == name.lower()).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_ticket_category_form.html', category=None, form=request.form)

        db.session.add(TicketCategory(name=name, default_price=default_price))
        _log_activity('ticket_category_add', f'Added ticket category "{name}".')
        db.session.commit()
        flash(f'Added category "{name}".', 'success')
        return redirect(url_for('admin_ticket_categories'))

    return render_template('admin_ticket_category_form.html', category=None, form=None)


@app.route('/admin/ticket_categories/<int:category_id>/edit', methods=['GET', 'POST'])
@require_permission('tickets')
def admin_ticket_category_edit(category_id):
    category = TicketCategory.query.get_or_404(category_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        is_active = bool(request.form.get('is_active'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_ticket_category_form.html', category=category, form=None)
        if TicketCategory.query.filter(db.func.lower(TicketCategory.name) == name.lower(),
                                        TicketCategory.id != category_id).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_ticket_category_form.html', category=category, form=None)

        category.name = name
        category.default_price = default_price
        category.is_active = is_active
        _log_activity('ticket_category_edit', f'Edited ticket category "{name}".')
        db.session.commit()
        flash(f'Updated category "{name}".', 'success')
        return redirect(url_for('admin_ticket_categories'))

    return render_template('admin_ticket_category_form.html', category=category, form=None)


@app.route('/admin/ticket_categories/<int:category_id>/delete', methods=['POST'])
@require_permission('tickets')
def admin_ticket_category_delete(category_id):
    category = TicketCategory.query.get_or_404(category_id)
    in_use = Ticket.query.filter_by(category_id=category_id).count()
    if in_use:
        flash(f'Cannot delete "{category.name}" — {in_use} ticket(s) still reference it. Deactivate it instead.', 'error')
        return redirect(url_for('admin_ticket_categories'))
    name = category.name
    db.session.delete(category)
    _log_activity('ticket_category_delete', f'Deleted ticket category "{name}".')
    db.session.commit()
    flash(f'Deleted category "{name}".', 'success')
    return redirect(url_for('admin_ticket_categories'))


# ─── Activity Log ─────────────────────────────────────────────────────────────

ACTIVITY_LOG_ACTIONS = [
    'device_add', 'device_edit', 'device_delete', 'device_assign', 'device_unassign', 'device_status',
    'registry_csv_import', 'registry_set_sites',
    'person_add', 'person_edit', 'person_delete', 'person_reactivate', 'people_csv_import', 'people_graduate',
    'loaner_toggle', 'loaner_checkout', 'loaner_checkin', 'reminders_send',
    'incident_add', 'incident_delete', 'fee_paid', 'fee_edit',
    'repair_send', 'repair_return', 'repair_edit',
    'kiosk_enroll', 'kiosk_revoke',
    'user_add', 'user_edit', 'user_delete',
    'site_add', 'site_edit', 'site_delete',
    'ticket_add', 'ticket_edit', 'ticket_status', 'ticket_assign', 'ticket_comment',
    'ticket_charge_add', 'ticket_charge_delete',
    'ticket_category_add', 'ticket_category_edit', 'ticket_category_delete',
    'device_model_add', 'device_model_edit', 'device_model_delete',
    'asset_number_range_add', 'asset_number_range_edit', 'asset_number_range_delete',
    'repair_category_add', 'repair_category_edit', 'repair_category_delete',
    'branding_edit', 'email_template_edit',
    'custom_field_add', 'custom_field_delete',
    'google_field_mapping_add', 'google_field_mapping_delete', 'google_field_sync',
    'org_unit_refresh', 'org_unit_classify',
    'loaner_ou_push',
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
    search = request.args.get('q', '').strip()
    action_filter = request.args.get('action', '').strip()
    since_str = request.args.get('since', '').strip()
    until_str = request.args.get('until', '').strip()
    sort_dir = request.args.get('dir', 'desc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'

    query = _scope_activity_log(ActivityLog.query, _current_site_ids())
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(ActivityLog.actor_label.ilike(like), ActivityLog.summary.ilike(like)))
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
    query = query.order_by(ActivityLog.timestamp.asc() if sort_dir == 'asc' else ActivityLog.timestamp.desc())

    pagination = query.paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_activity.html', pagination=pagination, actions=ACTIVITY_LOG_ACTIONS,
                           search=search, action_filter=action_filter,
                           since=since_str, until=until_str, sort_dir=sort_dir)


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