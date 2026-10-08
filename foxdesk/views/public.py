"""Pages: public."""
from datetime import datetime
from flask import jsonify, redirect, render_template, request
from sqlalchemy import text
from foxdesk.core import app, db, logger
from foxdesk.models import Asset, AssetRegistry, Event
from foxdesk.services.util import resolve_scan
from foxdesk.services.auth import (
    _admin_session_active,
    _current_site_ids,
    _current_user,
    _post_login_redirect,
    api_login_required,
    kiosk_or_api_permission_required,
    kiosk_or_login_required,
    kiosk_or_permission_required,
    login_required,
)


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


@app.route('/')
@kiosk_or_login_required
def index():
    """The big-button landing page is for kiosks (Check In / Check Out /
    Report a Problem / Submit a Ticket). A logged-in user has all of that in
    their own nav, so they go straight to their normal landing page."""
    if _admin_session_active():
        return redirect(_post_login_redirect(_current_user()))
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
