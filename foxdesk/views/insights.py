"""Pages: insights."""
import csv
import io
from collections import defaultdict
from datetime import datetime
from flask import Response, abort, flash, redirect, render_template, request, url_for
from foxdesk.core import GOOGLE_SYNC_ENABLED, app, db
from foxdesk.models import ASSET_STATUSES, Asset, AssetRegistry, AuditScan, DEVICE_TYPES, Person, SigninReview
from foxdesk.services.util import resolve_scan
from foxdesk.services.auth import _current_actor, _current_site_ids, _log_activity, require_permission
from foxdesk.services.scoping import _filter_registry_by_status, _scope_people, _scope_registry
from foxdesk.services.assignments import _assign_asset_to_person
from foxdesk.automation.engine import emit
from foxdesk.services.reports import (
    DATA_QUALITY_ROW_LIMIT,
    SIGNIN_CATEGORIES,
    SIGNIN_HANDOFF_GRACE_DAYS,
    SIGNIN_SEVERITY_ORDER,
    SIGNIN_WINDOW_DAYS_CHOICES,
    _data_quality_checks,
    _signin_mismatches,
    ASSIGN_SKIP_NOTE,
    ASSIGN_TIERS,
    _assignment_proposals,
    _refresh_forecast,
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


@app.route('/admin/refresh_forecast')
@require_permission('devices')
def admin_refresh_forecast():
    """When Chromebooks stop getting ChromeOS updates (Google's AUE date),
    by school year: what to budget for, and what's already past it."""
    forecast = _refresh_forecast(_current_site_ids())
    if request.args.get('format') == 'csv':
        import csv
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['asset_tag', 'serial_number', 'model', 'updates_end', 'status', 'assigned_to', 'site'])
        for row, asset, model in forecast['past_in_use']:
            writer.writerow([row.asset_tag, row.serial_number or '', model, asset.google_aue_date.isoformat(),
                             asset.status or '', asset.assigned_to.full_name if asset.assigned_to else '',
                             row.site.name if row.site else ''])
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=past-auto-update-expiration.csv'})
    peak = max([y['count'] for y in forecast['years']] + [1])
    return render_template('admin_refresh_forecast.html', forecast=forecast, peak=peak)


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


ASSIGN_BATCH_LIMIT = 200  # per click, to stay well inside the request timeout


@app.route('/admin/assign_from_google', methods=['GET', 'POST'])
@require_permission('devices')
def admin_assign_from_google():
    """Unassigned devices someone is clearly using, with the person to give
    each one to (the latest Google sign-in). Assign the ones you agree with,
    or skip them so they don't come back."""
    site_ids = _current_site_ids()
    window_days = _signin_window_days()
    if request.method == 'POST':
        picks = request.form.getlist('pick')
        if not picks:
            flash('Tick the devices first.', 'error')
            return redirect(request.full_path)
        if len(picks) > ASSIGN_BATCH_LIMIT:
            flash(f'Pick at most {ASSIGN_BATCH_LIMIT} at a time.', 'error')
            return redirect(request.full_path)
        skipping = request.form.get('action') == 'skip'
        _, actor_label, _ = _current_actor()
        done, problems = 0, []
        for pick in picks:
            try:
                tag, person_id, email = pick.split('|', 2)
                person_id = int(person_id)
            except ValueError:
                continue
            registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=tag).first()
            if not registry_row:
                continue
            if skipping:
                if not SigninReview.query.filter_by(asset_tag=tag, signin_email=email.lower()).first():
                    db.session.add(SigninReview(asset_tag=tag, signin_email=email.lower(), note=ASSIGN_SKIP_NOTE,
                                                reviewed_by=actor_label))
                    _log_activity('signin_review', f'Skipped assigning {tag} to {email} (from Google sign-ins).',
                                  site_id=registry_row.site_id)
                done += 1
                continue
            person = _scope_people(Person.query, site_ids).filter_by(id=person_id, is_active=True).first()
            asset = Asset.query.filter_by(asset_tag=tag).first()
            if not person or (asset and asset.assigned_to_id):
                problems.append(tag)
                continue
            status, message = _assign_asset_to_person(tag, person)  # commits each one
            if status == 'assigned':
                emit('device.assigned', registry_row, person=person)
                done += 1
            else:
                problems.append(tag)
        db.session.commit()
        verb = 'Skipped' if skipping else 'Assigned'
        flash(f'{verb} {done} device{"" if done == 1 else "s"}.'
              + (f' Left alone (already assigned, or the person is no longer active): {", ".join(problems[:10])}'
                 + ('…' if len(problems) > 10 else '') if problems else ''), 'success' if done else 'error')
        return redirect(request.full_path)

    proposals = _assignment_proposals(site_ids, window_days)
    counts = {t: 0 for t in ASSIGN_TIERS}
    for p in proposals:
        counts[p['tier']] += 1
    tier = request.args.get('tier', 'high')
    tier = tier if tier in ASSIGN_TIERS else 'high'
    return render_template('admin_assign_from_google.html', proposals=[p for p in proposals if p['tier'] == tier],
                           tier=tier, tiers=ASSIGN_TIERS, counts=counts, window_days=window_days,
                           window_choices=SIGNIN_WINDOW_DAYS_CHOICES, batch_limit=ASSIGN_BATCH_LIMIT,
                           has_google_data=Asset.query.filter(Asset.google_last_activity.isnot(None)).first() is not None)


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
