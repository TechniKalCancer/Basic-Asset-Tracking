from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone, timedelta
from functools import wraps
import os
import csv
import io
import time
from collections import defaultdict
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL') or 'sqlite:///assets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')

SESSION_TIMEOUT_MINUTES = 15
WARNING_BEFORE_SECONDS  = 120  # warn 2 min before expiry

ADMIN_PASSWORD_HASH = generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'admin123'))

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
    id           = db.Column(db.Integer, primary_key=True)
    asset_tag    = db.Column(db.String(120), unique=True, nullable=False, index=True)
    serial_number = db.Column(db.String(120), unique=True, nullable=True, index=True)
    description  = db.Column(db.String(255), nullable=True)

    def to_dict(self):
        return {
            'asset_tag': self.asset_tag,
            'serial_number': self.serial_number,
            'description': self.description,
        }


class Asset(db.Model):
    """
    Tracks the current check-in/out status of an asset (keyed by asset_tag).
    is_valid = False means the scan happened before the asset was in the registry;
    it gets healed automatically when a CSV import adds that asset_tag.
    """
    __tablename__ = 'asset'
    id           = db.Column(db.Integer, primary_key=True)
    asset_tag    = db.Column(db.String(120), unique=True, nullable=False, index=True)
    check_in     = db.Column(db.DateTime, nullable=True)
    check_out    = db.Column(db.DateTime, nullable=True)
    is_valid     = db.Column(db.Boolean, default=False, nullable=False)

    def to_dict(self):
        return {
            'asset_tag': self.asset_tag,
            'check_in':  self.check_in.isoformat() if self.check_in else None,
            'check_out': self.check_out.isoformat() if self.check_out else None,
            'is_valid':  self.is_valid,
        }


class Event(db.Model):
    __tablename__ = 'event'
    id        = db.Column(db.Integer, primary_key=True)
    asset_tag = db.Column(db.String(120), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    action    = db.Column(db.String(50), nullable=False)
    scanned_value = db.Column(db.String(120), nullable=True)   # raw scan (tag or serial)
    scan_type     = db.Column(db.String(20), nullable=True)    # 'asset_tag' | 'serial'

    def to_dict(self):
        return {
            'asset_tag':    self.asset_tag,
            'timestamp':    self.timestamp.isoformat(),
            'action':       self.action,
            'scanned_value': self.scanned_value,
            'scan_type':    self.scan_type,
        }


with app.app_context():
    db.create_all()


# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('Please log in to access the admin panel.', 'error')
            return redirect(url_for('admin_login'))

        # Check session timeout
        last_active = session.get('last_active')
        if last_active:
            elapsed = datetime.now(timezone.utc).timestamp() - last_active
            if elapsed > SESSION_TIMEOUT_MINUTES * 60:
                session.clear()
                flash('Your session expired after 15 minutes of inactivity.', 'error')
                return redirect(url_for('admin_login'))

        # Refresh last active timestamp on every admin request
        session['last_active'] = datetime.now(timezone.utc).timestamp()
        session.permanent = True
        return f(*args, **kwargs)
    return decorated


# ─── Auth ─────────────────────────────────────────────────────────────────────

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

        password = request.form.get('password', '')
        if check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.clear()
            session['admin_logged_in'] = True
            session['last_active'] = datetime.now(timezone.utc).timestamp()
            session.permanent = True
            return redirect(url_for('admin_panel'))

        _record_attempt(ip)
        attempts_left = MAX_ATTEMPTS - len(_login_attempts[ip])
        flash(f'Invalid password. {attempts_left} attempt{"s" if attempts_left != 1 else ""} remaining.', 'error')

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
    return render_template('admin_panel.html',
                           registry_count=registry_count,
                           orphan_count=orphan_count)


@app.route('/admin/upload_csv', methods=['POST'])
@login_required
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

        tag_col    = next((c for c in TAG_COLS    if c in normalized_headers), None)
        serial_col = next((c for c in SERIAL_COLS if c in normalized_headers), None)
        desc_col   = next((c for c in DESC_COLS   if c in normalized_headers), None)

        if not tag_col:
            flash(f'CSV must have an "Asset ID" or "asset_tag" column. Found: {", ".join(normalized_headers)}', 'error')
            return redirect(url_for('admin_panel'))

        rows = list(reader)

        # Wipe old registry and replace
        AssetRegistry.query.delete()
        db.session.flush()

        imported = 0
        skipped  = 0
        seen_tags    = set()
        seen_serials = set()

        def clean(val):
            """Return None if value is empty, '0', or whitespace."""
            v = (val or '').strip()
            return None if (not v or v == '0') else v

        for row in rows:
            tag    = clean(row.get(tag_col, ''))
            serial = clean(row.get(serial_col, '')) if serial_col else None
            desc   = clean(row.get(desc_col, ''))   if desc_col   else None

            # If asset_id is missing but serial exists, use serial as the tag
            if not tag and serial:
                tag = serial

            # Skip completely empty rows
            if not tag:
                skipped += 1
                continue

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
            ))
            imported += 1

        db.session.commit()
        healed = heal_orphans()

        msg = f'Imported {imported} assets.'
        if skipped:
            msg += f' Skipped {skipped} duplicate/invalid rows.'
        if healed:
            msg += f' Healed {healed} previously-unknown asset record(s).'
        flash(msg, 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Import failed: {e}', 'error')

    return redirect(url_for('admin_panel'))


@app.route('/admin/registry')
@login_required
def admin_registry():
    page     = request.args.get('page', 1, type=int)
    per_page = 50
    query    = AssetRegistry.query.order_by(AssetRegistry.asset_tag)
    search   = request.args.get('q', '').strip()
    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                AssetRegistry.asset_tag.ilike(like),
                AssetRegistry.serial_number.ilike(like),
                AssetRegistry.description.ilike(like),
            )
        )
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('admin_registry.html', pagination=pagination, search=search)


@app.route('/admin/orphans')
@login_required
def admin_orphans():
    orphans = Asset.query.filter_by(is_valid=False).order_by(Asset.asset_tag).all()
    return render_template('admin_orphans.html', orphans=orphans)


# ─── Scan / Check-in / Check-out API ─────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
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

    # Validate length — must be 5-6 digits (asset tag) or 10 chars (serial)
    if len(scan_value) not in (5, 6, 10):
        return jsonify({
            'error': f'Invalid scan: "{scan_value}" is {len(scan_value)} characters. Asset tags must be 5-6 digits, serials must be 10 characters.',
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
        print(f'Scan error: {e}')
        return jsonify({'error': 'Internal Server Error'}), 500


# ─── Public API ───────────────────────────────────────────────────────────────

@app.route('/api/assets', methods=['GET'])
def get_assets():
    try:
        assets = Asset.query.order_by(Asset.asset_tag).all()
        return jsonify([a.to_dict() for a in assets])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/assets/<string:asset_tag>/history', methods=['GET'])
def get_asset_history(asset_tag):
    events = Event.query.filter_by(asset_tag=asset_tag).order_by(Event.timestamp.desc()).all()
    if not events:
        return jsonify({'message': f'No history found for {asset_tag}'}), 404
    return jsonify([e.to_dict() for e in events])


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/checkin')
def checkin_page():
    recent = Event.query.filter_by(action='checkin').order_by(Event.timestamp.desc()).limit(10).all()
    return render_template('scan.html', action='checkin', title='Check In', recent_events=recent)


@app.route('/checkout')
def checkout_page():
    recent = Event.query.filter_by(action='checkout').order_by(Event.timestamp.desc()).limit(10).all()
    return render_template('scan.html', action='checkout', title='Check Out', recent_events=recent)


@app.route('/asset_history')
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
    app.run(host='0.0.0.0', port=8081, debug=True)