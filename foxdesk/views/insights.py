"""Pages: insights."""
import csv
import io
from collections import defaultdict
from datetime import datetime
from flask import abort, flash, redirect, render_template, request, url_for
from foxdesk.core import GOOGLE_SYNC_ENABLED, app, db
from foxdesk.models import ASSET_STATUSES, Asset, AssetRegistry, AuditScan, DEVICE_TYPES, SigninReview
from foxdesk.services.util import resolve_scan
from foxdesk.services.auth import _current_actor, _current_site_ids, _log_activity, require_permission
from foxdesk.services.scoping import _filter_registry_by_status, _scope_registry
from foxdesk.services.reports import (
    DATA_QUALITY_ROW_LIMIT,
    SIGNIN_CATEGORIES,
    SIGNIN_HANDOFF_GRACE_DAYS,
    SIGNIN_SEVERITY_ORDER,
    SIGNIN_WINDOW_DAYS_CHOICES,
    _data_quality_checks,
    _signin_mismatches,
    _signin_window_days,
)


@app.route('/admin/data_quality')
@require_permission('devices')
def admin_data_quality():
    checks = _data_quality_checks(_current_site_ids())
    export_key = request.args.get('export')
    if export_key:
        check = next((c for c in checks if c['key'] == export_key), None)
        if not check:
            abort(404)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(check['columns'])
        for row in check['rows']:
            writer.writerow(row['cells'])
        response = app.response_class(out.getvalue(), mimetype='text/csv')
        response.headers['Content-Disposition'] = f'attachment; filename=data_quality_{export_key}.csv'
        return response
    return render_template('admin_data_quality.html', checks=checks, row_limit=DATA_QUALITY_ROW_LIMIT)


@app.route('/admin/signin_mismatches')
@require_permission('devices')
def admin_signin_mismatches():
    window_days = _signin_window_days()
    show_reviewed = request.args.get('reviewed') == '1'
    severity = request.args.get('severity', '').strip()
    mismatches = _signin_mismatches(_current_site_ids(), window_days, include_reviewed=show_reviewed)
    counts = defaultdict(int)
    for m in mismatches:
        counts[m['severity']] += 1
    if severity in SIGNIN_SEVERITY_ORDER:
        mismatches = [m for m in mismatches if m['severity'] == severity]
    has_google_data = Asset.query.filter(Asset.google_last_activity.isnot(None)).first() is not None
    return render_template('admin_signin_mismatches.html', mismatches=mismatches, counts=counts,
                           window_days=window_days, window_choices=SIGNIN_WINDOW_DAYS_CHOICES,
                           show_reviewed=show_reviewed, severity=severity, categories=SIGNIN_CATEGORIES,
                           has_google_data=has_google_data, google_sync_enabled=GOOGLE_SYNC_ENABLED,
                           grace_days=SIGNIN_HANDOFF_GRACE_DAYS)


@app.route('/admin/signin_mismatches/review', methods=['POST'])
@require_permission('devices')
def admin_signin_mismatch_review():
    """Marks one (device, account) mismatch as reviewed/OK, or with
    action=reopen puts it back on the list."""
    asset_tag = request.form.get('asset_tag', '').strip()
    signin_email = request.form.get('signin_email', '').strip().lower()
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    existing = SigninReview.query.filter_by(asset_tag=asset_tag, signin_email=signin_email).first()
    try:
        if request.form.get('action') == 'reopen':
            if existing:
                db.session.delete(existing)
                _log_activity('signin_review', f'Reopened sign-in flag: {signin_email} on {asset_tag}.', site_id=registry_row.site_id)
            flash(f'{signin_email} on {asset_tag} is back on the list.', 'success')
        else:
            _, actor_label, _ = _current_actor()
            note = request.form.get('note', '').strip()[:255] or None
            if not existing:
                db.session.add(SigninReview(asset_tag=asset_tag, signin_email=signin_email, note=note, reviewed_by=actor_label))
            _log_activity('signin_review', f'Marked sign-in OK: {signin_email} on {asset_tag}'
                           f'{" — " + note if note else ""}.', site_id=registry_row.site_id)
            flash(f'Marked {signin_email} on {asset_tag} as reviewed. It\'ll only come back if a different account signs in.', 'success')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'Could not save: {e}', 'error')
    return redirect(request.referrer or url_for('admin_signin_mismatches'))


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
