"""Pages: Dell warranty lookup (Settings → Dell warranty)."""
from datetime import date, timedelta

from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.models import AssetRegistry, DeviceRecord
from foxdesk.integrations.dell import (
    WarrantyError, candidates, dell_credentials, describe_summary, get_token, run_dell_lookup, save_credentials,
    warranty_settings,
)
from foxdesk.services.auth import _log_activity, require_super_admin
from foxdesk.services.scheduler import SYNC_SCHEDULE_INTERVALS, _get_or_create_sync_schedule

LOOKUP_NOW_LIMIT = 500  # per click; the scheduled run does the rest


@app.route('/admin/warranty')
@require_super_admin
def admin_warranty():
    settings = warranty_settings()
    db.session.commit()
    client_id, secret, sources = dell_credentials()
    today = date.today()
    with_date = AssetRegistry.query.filter(AssetRegistry.warranty_expiration.isnot(None))
    stats = dict(
        with_date=with_date.count(),
        expired=with_date.filter(AssetRegistry.warranty_expiration < today).count(),
        expiring=with_date.filter(AssetRegistry.warranty_expiration >= today,
                                  AssetRegistry.warranty_expiration <= today + timedelta(days=60)).count(),
        looked_up=DeviceRecord.query.filter_by(source='dell').count(),
        due=len(candidates()) if client_id and secret else None,
    )
    return render_template('admin_warranty.html', settings=settings, client_id=client_id, configured=bool(client_id and secret),
                           sources=sources, stats=stats, describe=describe_summary, limit=LOOKUP_NOW_LIMIT,
                           schedule=_get_or_create_sync_schedule('dell'), intervals=SYNC_SCHEDULE_INTERVALS)


@app.route('/admin/warranty/credentials', methods=['POST'])
@require_super_admin
def admin_warranty_credentials():
    save_credentials(request.form.get('client_id'), request.form.get('client_secret'))
    _log_activity('warranty_settings', 'Saved the Dell TechDirect API key'
                  + (' (new secret)' if request.form.get('client_secret') else '') + '.')
    db.session.commit()
    flash('Saved. Click Test to check it with Dell.', 'success')
    return redirect(url_for('admin_warranty'))


@app.route('/admin/warranty/test', methods=['POST'])
@require_super_admin
def admin_warranty_test():
    try:
        get_token()
        flash('Dell accepted the key.', 'success')
    except WarrantyError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_warranty'))


@app.route('/admin/warranty/run', methods=['POST'])
@require_super_admin
def admin_warranty_run():
    try:
        summary = run_dell_lookup(limit=LOOKUP_NOW_LIMIT, force=request.form.get('force') == '1')
        flash(describe_summary(summary), 'success')
    except WarrantyError as e:
        db.session.rollback()
        settings = warranty_settings()
        settings.last_error = str(e)
        db.session.commit()
        flash(str(e), 'error')
    return redirect(url_for('admin_warranty'))
