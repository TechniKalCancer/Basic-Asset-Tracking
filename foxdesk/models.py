"""SQLAlchemy models and the enums that belong to them."""
from datetime import datetime
from foxdesk.core import db


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
    ticket_received_subject     = db.Column(db.String(200), nullable=True)
    ticket_received_body        = db.Column(db.Text, nullable=True)
    ticket_reply_subject        = db.Column(db.String(200), nullable=True)
    ticket_reply_body           = db.Column(db.Text, nullable=True)
    ticket_resolved_subject     = db.Column(db.String(200), nullable=True)
    ticket_resolved_body        = db.Column(db.Text, nullable=True)
    damage_notice_subject       = db.Column(db.String(200), nullable=True)
    damage_notice_body          = db.Column(db.Text, nullable=True)
    # Master switch for the automatic requester emails (received/resolved).
    # Replies are an explicit per-comment choice, so they ignore this.
    ticket_notifications_enabled = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
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


class KaceFieldMapping(db.Model):
    """
    Maps one field from a KACE SMA device inventory record onto one target
    field on AssetRegistry, applied by _run_kace_device_sync() at
    /admin/kace_field_mapping. kace_field is a key from
    KACE_DEVICE_FIELDS (e.g. 'SYSTEM_NAME', 'OS_NAME') — flat, unlike
    GoogleFieldMapping.google_field, since KACE's inventory grid returns a
    flat record with no nesting. target_field is either a real column name
    (from DEVICE_SYNC_TARGET_FIELDS) or 'custom:<key>' referencing a
    CustomField, same convention as GoogleFieldMapping. Always maps onto
    AssetRegistry — KACE has no Person-equivalent data, so unlike
    GoogleFieldMapping there's no entity_type to track.
    """
    __tablename__ = 'kace_field_mapping'
    id           = db.Column(db.Integer, primary_key=True)
    kace_field   = db.Column(db.String(80), nullable=False)
    target_field = db.Column(db.String(80), nullable=False)
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


class SyncSchedule(db.Model):
    """
    One row per Google sync type ('person'/'device'), configuring whether
    _run_google_people_sync()/_run_google_device_sync() should also run on
    their own on a timer, not just via the manual "Run Sync Now" button on
    /admin/google_field_mapping. Checked/run by the background loop started
    at the bottom of this file (see _scheduled_sync_loop) — same
    once-an-hour-check pattern as the loaner reminder loop, with
    last_run_at doubling as the idempotency gate across gunicorn workers.
    """
    __tablename__ = 'sync_schedule'
    id                = db.Column(db.Integer, primary_key=True)
    sync_type         = db.Column(db.String(20), unique=True, nullable=False)  # 'person' | 'device'
    enabled           = db.Column(db.Boolean, nullable=False, default=False)
    interval_hours    = db.Column(db.Integer, nullable=False, default=24)
    last_run_at       = db.Column(db.DateTime, nullable=True)
    last_run_summary  = db.Column(db.String(255), nullable=True)


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
    loaner_label  = db.Column(db.String(80), nullable=True)  # e.g. "Front Office #3" — physically identifies this specific loaner, printed on its Dymo label instead of "Unassigned"
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
    google_recent_users = db.Column(db.JSON, nullable=True)  # Google's recentUsers emails, most recent first (≤5) — see _signin_mismatches()
    google_last_activity = db.Column(db.DateTime, nullable=True)  # device's own lastSync in Google (UTC) — when it was last powered on and online, not when we last pulled it
    google_enabled     = db.Column(db.Boolean, nullable=True)  # last known enabled/disabled state — set by the loaner auto-disable sync, the per-device/bulk Google sync, and the manual toggle button

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
            'google_enabled':      self.google_enabled,
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
    guardian_name  = db.Column(db.String(160), nullable=True)
    guardian_email = db.Column(db.String(160), nullable=True)  # where damage notices go — students only, usually filled from the SIS export via People CSV import
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    custom_fields = db.Column(db.JSON, nullable=True)  # {field_key: value, ...} — see CustomField/GoogleFieldMapping

    # Populated by the "Sync from Google" button on the person edit page —
    # same cached-not-live pattern as Asset.google_org_unit below, so the
    # edit page never has to make a live API call just to render.
    google_org_unit     = db.Column(db.String(255), nullable=True)
    google_last_sync_at = db.Column(db.DateTime, nullable=True)

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


# Where a named User lands right after logging in — key into this dict,
# stored on User.default_landing. 'dashboard' is the long-standing default;
# 'loaners' exists for an admin whose day-to-day job is really the loaner
# pool and would rather skip the Dashboard detour every time.
LANDING_PAGES = {'dashboard': 'Dashboard', 'loaners': 'Loaner Pool'}


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
    default_landing = db.Column(db.String(20), nullable=False, default='dashboard')  # key into LANDING_PAGES — where login sends this user
    # A super admin's own standing "which site am I looking at" preference —
    # None means all sites (this app's normal super-admin default). Only
    # meaningful for a super admin; a site-scoped user's view is always just
    # their own `sites` list regardless of this. Set via the switcher in the
    # nav (/admin/set_active_site), not this form — see _current_site_ids().
    default_site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)

    sites = db.relationship('Site', secondary='user_site', backref='users')
    default_site = db.relationship('Site', foreign_keys=[default_site_id])


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
    guardian_notified_at = db.Column(db.DateTime, nullable=True)  # last damage notice emailed to the student's guardian

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


HELP_ARTICLE_TYPES = {'faq': 'FAQ', 'howto': 'How-To Guide'}


class HelpArticle(db.Model):
    """
    One entry in the in-app Help page (/help) — either a short FAQ
    question/answer or a longer How-To guide, distinguished by article_type.
    Admin-editable at /admin/help so the content stays current as features
    change, rather than being hardcoded into a template. is_active=False
    hides an entry from /help without losing it (e.g. seasonal content, or
    a draft not ready yet). sort_order controls display order within its
    type — ties break by title, so a fresh entry (sort_order=0) is usable
    immediately without an admin having to renumber anything.
    """
    __tablename__ = 'help_article'
    id           = db.Column(db.Integer, primary_key=True)
    article_type = db.Column(db.String(20), nullable=False)  # 'faq' | 'howto'
    title        = db.Column(db.String(200), nullable=False)  # the question (FAQ) or guide title (How-To)
    body         = db.Column(db.Text, nullable=False)  # the answer (FAQ) or step-by-step content (How-To)
    sort_order   = db.Column(db.Integer, nullable=False, default=0)
    is_active    = db.Column(db.Boolean, nullable=False, default=True)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    """A note on a Ticket. There's no submitter-facing ticket portal/login in
    this app (public forms are anonymous + person-search based, not
    accounts), so comments are internal by default — emailed_to_requester
    marks the ones a tech chose to also send to the requester as a reply."""
    __tablename__ = 'ticket_comment'
    id          = db.Column(db.Integer, primary_key=True)
    ticket_id   = db.Column(db.Integer, db.ForeignKey('ticket.id'), nullable=False, index=True)
    body        = db.Column(db.Text, nullable=False)
    author_label = db.Column(db.String(160), nullable=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    emailed_to_requester = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())


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


# Action types a TicketAutomation/PendingDeviceAction can run — structured as
# a dict (rather than a bare string check) so one more automated action is
# just one more entry plus one more branch in _execute_device_automation_action,
# not a schema change.
AUTOMATION_ACTIONS = {
    'profile_clear': 'Profile Clear (wipe local users)',
    'disable_google': 'Disable in Google Workspace',
    'send_to_repair': 'Send to Repair',
    'move_device': 'Move to Org Unit',
}


class TicketAutomation(db.Model):
    """
    Links a Ticket Category to an automated device action — e.g. selecting
    "Cryptohome Error" on a ticket that has a device attached can trigger a
    Google Workspace profile clear on that device, instead of a tech having
    to remember to do it by hand. One automation per category (unique), so
    picking the category is unambiguous about what it'll do.

    require_confirmation gates whether the action fires the moment the
    ticket is created or gets staged as a PendingDeviceAction for a
    one-click admin confirm first. This is per-automation and
    admin-configurable rather than a single global on/off switch — the
    underlying action (WIPE_USERS) clears every local profile on the
    device and can't be undone, so some districts/categories may want a
    human to greenlight it and others may not.

    Only ever fires when the triggering ticket has an asset_tag (a device
    attached) — nothing to act on otherwise.
    """
    __tablename__ = 'ticket_automation'
    id                    = db.Column(db.Integer, primary_key=True)
    ticket_category_id    = db.Column(db.Integer, db.ForeignKey('ticket_category.id'), nullable=False, unique=True)
    action_type           = db.Column(db.String(30), nullable=False)  # key into AUTOMATION_ACTIONS
    require_confirmation  = db.Column(db.Boolean, nullable=False, default=True)
    is_active             = db.Column(db.Boolean, nullable=False, default=True)
    created_at            = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Only meaningful for specific action_types — nullable/unused otherwise,
    # same reasoning as Repair.repair_category_id being optional.
    repair_category_id    = db.Column(db.Integer, db.ForeignKey('repair_category.id'), nullable=True)  # 'send_to_repair'
    target_org_unit_path  = db.Column(db.String(255), nullable=True)  # 'move_device'

    ticket_category  = db.relationship('TicketCategory')
    repair_category  = db.relationship('RepairCategory')


class PendingDeviceAction(db.Model):
    """
    A device action staged by a TicketAutomation with require_confirmation
    on, waiting for an admin to confirm or dismiss it before it actually
    runs. Surfaced on both the triggering ticket's page and the device's
    own page, since a tech might be looking at either one first. Resolved
    rows (confirmed/dismissed/failed) are kept rather than deleted, same
    reasoning as ActivityLog never being trimmed — a resolved automation
    shouldn't look unhandled after the fact.
    """
    __tablename__ = 'pending_device_action'
    id                = db.Column(db.Integer, primary_key=True)
    ticket_id         = db.Column(db.Integer, db.ForeignKey('ticket.id'), nullable=True)
    asset_tag         = db.Column(db.String(120), nullable=False, index=True)
    action_type       = db.Column(db.String(30), nullable=False)
    status            = db.Column(db.String(20), nullable=False, default='pending')  # 'pending' | 'confirmed' | 'dismissed' | 'failed'
    created_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    resolved_at       = db.Column(db.DateTime, nullable=True)
    resolved_by_label = db.Column(db.String(160), nullable=True)
    error_message     = db.Column(db.String(255), nullable=True)
    # Set when an automation rule (rather than a legacy TicketAutomation)
    # staged this — params carries that rule's settings for the action
    # (repair_category, org_unit) so confirming later runs it the same way.
    rule_id           = db.Column(db.Integer, db.ForeignKey('automation_rule.id'), nullable=True)
    params            = db.Column(db.JSON, nullable=True)

    ticket = db.relationship('Ticket')


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


IDENTITY_SOURCES = {
    'google': 'Google Workspace',
    'ad': 'Active Directory',
    'entra': 'Microsoft Entra ID',
    'powerschool': 'PowerSchool',
    'alias': 'Other email',
}


class PersonIdentity(db.Model):
    """
    One account a person has in some other system — a Google Workspace
    user, an AD or Entra account, a PowerSchool record, or just an extra
    email. A person has one profile and any number of these; syncs attach
    to an existing person (by account, then email, then student/staff ID)
    instead of creating a second profile.

    person_id is NULL for an account a sync couldn't place with confidence:
    those wait on Accounts to Review (suggested_person_id holds an
    exact-name candidate, never auto-linked, since two students can share a
    name). review_status='ignored' hides one there for good — e.g. Google
    accounts pre-provisioned for students who never enrolled.
    """
    __tablename__ = 'person_identity'
    id             = db.Column(db.Integer, primary_key=True)
    person_id      = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True, index=True)
    source         = db.Column(db.String(20), nullable=False)
    external_key   = db.Column(db.String(255), nullable=False)  # stable ID in that system (Google user id, AD objectGUID, Entra object id, PowerSchool student number) — or the email for an alias
    email          = db.Column(db.String(255), nullable=True, index=True)  # always lowercase
    username       = db.Column(db.String(255), nullable=True)   # sAMAccountName / UPN / login name
    display_name   = db.Column(db.String(255), nullable=True)
    directory_path = db.Column(db.String(512), nullable=True)   # Google org unit, AD distinguished name, Entra department
    enabled        = db.Column(db.Boolean, nullable=True)
    suggested_person_id = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=True)
    review_status  = db.Column(db.String(20), nullable=True)    # None | 'ignored'
    raw            = db.Column(db.JSON, nullable=True)          # extra attributes from the source, for display only
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    person = db.relationship('Person', foreign_keys=[person_id], backref=db.backref('identities', lazy='select'))
    suggested_person = db.relationship('Person', foreign_keys=[suggested_person_id])

    __table_args__ = (db.UniqueConstraint('source', 'external_key', name='uq_person_identity_source_key'),)

    @property
    def source_label(self):
        return IDENTITY_SOURCES.get(self.source, self.source)


DEVICE_RECORD_SOURCES = {
    'google': 'Google Workspace',
    'kace': 'KACE SMA',
    'ad': 'Active Directory',
    'entra': 'Microsoft Entra ID',
    'intune': 'Microsoft Intune',
    'jamf': 'Jamf Pro',
}
JOIN_TYPES = {'ad': 'AD-joined', 'entra': 'Entra-joined', 'hybrid': 'Hybrid-joined', 'workgroup': 'Workgroup'}


class DeviceRecord(db.Model):
    """
    One record for a device in some other system — an AD computer object,
    an Entra device, an Intune or Jamf managed device, a KACE inventory row,
    a Google Chromebook. A device has one registry row and any number of
    these, matched by serial number first, then hostname.

    registry_id is NULL for a record no registry device matched; those wait
    on Computers to Review to be linked or added. join_type is what the
    source says about directory membership (see JOIN_TYPES) and drives the
    AD / Entra / hybrid / Workgroup view.
    """
    __tablename__ = 'device_record'
    id             = db.Column(db.Integer, primary_key=True)
    registry_id    = db.Column(db.Integer, db.ForeignKey('asset_registry.id'), nullable=True, index=True)
    source         = db.Column(db.String(20), nullable=False)
    external_key   = db.Column(db.String(255), nullable=False)  # AD objectGUID, Entra deviceId, Intune/Jamf/KACE/Google id
    hostname       = db.Column(db.String(255), nullable=True, index=True)
    serial_number  = db.Column(db.String(120), nullable=True, index=True)
    join_type      = db.Column(db.String(20), nullable=True)
    os_name        = db.Column(db.String(120), nullable=True)
    os_version     = db.Column(db.String(120), nullable=True)
    directory_path = db.Column(db.String(512), nullable=True)
    enabled        = db.Column(db.Boolean, nullable=True)
    review_status  = db.Column(db.String(20), nullable=True)    # None | 'ignored'
    raw            = db.Column(db.JSON, nullable=True)
    last_seen_at   = db.Column(db.DateTime, nullable=True)      # the device's own last logon/check-in in that system
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    registry_row = db.relationship('AssetRegistry', backref=db.backref('records', lazy='select'))

    __table_args__ = (db.UniqueConstraint('source', 'external_key', name='uq_device_record_source_key'),)

    @property
    def source_label(self):
        return DEVICE_RECORD_SOURCES.get(self.source, self.source)


class AutomationRule(db.Model):
    """
    "When <trigger>, if <conditions>, then <actions>" — see foxdesk/automation.
    conditions: [{"field": "ticket.priority", "op": "eq", "value": "urgent"}, ...]
    (all must match, or any with match='any'). actions: [{"type":
    "send_email", "params": {...}}, ...] run in order. site_id limits the
    rule to one school's subjects. template_key remembers which built-in
    template it started from (display only).
    """
    __tablename__ = 'automation_rule'
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(160), nullable=False)
    description  = db.Column(db.Text, nullable=True)
    trigger      = db.Column(db.String(60), nullable=False, index=True)
    match        = db.Column(db.String(3), nullable=False, default='all', server_default='all')
    conditions   = db.Column(db.JSON, nullable=False, default=list)
    actions      = db.Column(db.JSON, nullable=False, default=list)
    enabled      = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    site_id      = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)
    template_key = db.Column(db.String(60), nullable=True)
    run_count    = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    last_run_at  = db.Column(db.DateTime, nullable=True)
    created_by   = db.Column(db.String(160), nullable=True)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    site = db.relationship('Site')


class AutomationRun(db.Model):
    """
    One time a rule matched and ran (or would have, for a test). results is
    [{"action": ..., "status": "ok|skipped|error", "message": ...}].
    dedupe_key is set only for scheduled triggers ("repair:42") and is
    unique per rule, which is what lets every gunicorn worker run the same
    scheduled check without firing a rule twice for the same subject — the
    second insert simply fails.
    """
    __tablename__ = 'automation_run'
    id            = db.Column(db.Integer, primary_key=True)
    rule_id       = db.Column(db.Integer, db.ForeignKey('automation_rule.id'), nullable=False, index=True)
    trigger       = db.Column(db.String(60), nullable=False)
    subject_label = db.Column(db.String(255), nullable=True)
    dedupe_key    = db.Column(db.String(120), nullable=True)
    status        = db.Column(db.String(20), nullable=False)  # 'ran' | 'error'
    results       = db.Column(db.JSON, nullable=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    rule = db.relationship('AutomationRule', backref=db.backref('runs', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('rule_id', 'dedupe_key', name='uq_automation_run_dedupe'),)


class FeatureToggle(db.Model):
    """
    A district's on/off choice for one optional module (Settings → Features).
    No row means "use the module's default" from FEATURES in
    services/features.py, so adding a new module never needs a data
    migration — only an override is ever stored.
    """
    __tablename__ = 'feature_toggle'
    key        = db.Column(db.String(50), primary_key=True)
    enabled    = db.Column(db.Boolean, nullable=False)
    updated_by = db.Column(db.String(160), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class SigninReview(db.Model):
    """
    A tech's "looked at it, it's fine" on one Google sign-in mismatch (see
    _signin_mismatches) — e.g. a sibling sharing a device, a teacher who
    signed in to help. Keyed on (asset_tag, signin_email) rather than the
    device alone, so a *different* account showing up on the same device
    later is flagged fresh instead of staying silenced.
    """
    __tablename__ = 'signin_review'
    id           = db.Column(db.Integer, primary_key=True)
    asset_tag    = db.Column(db.String(120), nullable=False, index=True)
    signin_email = db.Column(db.String(255), nullable=False)
    note         = db.Column(db.String(255), nullable=True)
    reviewed_by  = db.Column(db.String(160), nullable=True)
    reviewed_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('asset_tag', 'signin_email', name='uq_signin_review_asset_email'),)


ATTACHMENT_OWNER_TYPES = ('incident', 'ticket', 'repair')


class Attachment(db.Model):
    """
    A photo (or PDF) attached to an Incident, Ticket, or Repair — damage
    evidence for a fee, a screenshot of an error, a vendor RMA slip. Stored
    in the database rather than on disk so it's covered by the same pg_dump
    backups as everything else and needs no extra Docker volume. Images are
    downscaled in the browser before upload (see static/js/attachments.js),
    so a typical phone photo lands around 200-400 KB, not 5+ MB.
    owner_type/owner_id is a loose polymorphic link (no FK) — the delete
    routes for each owner type clean up their own attachments.
    """
    __tablename__ = 'attachment'
    id            = db.Column(db.Integer, primary_key=True)
    owner_type    = db.Column(db.String(20), nullable=False)
    owner_id      = db.Column(db.Integer, nullable=False)
    filename      = db.Column(db.String(255), nullable=False)
    content_type  = db.Column(db.String(100), nullable=False)
    size_bytes    = db.Column(db.Integer, nullable=False)
    data          = db.deferred(db.Column(db.LargeBinary, nullable=False))  # deferred so listing attachments never loads the bytes
    uploaded_by   = db.Column(db.String(160), nullable=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (db.Index('ix_attachment_owner', 'owner_type', 'owner_id'),)

    @property
    def is_image(self):
        return self.content_type.startswith('image/')
