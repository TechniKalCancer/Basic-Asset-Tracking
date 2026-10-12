"""Pages: LibreNMS connection (Settings → LibreNMS)."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.integrations.librenms import (
    LibreNMSError, check_connection, describe_summary, librenms_config, librenms_settings, parse_site_rules,
    run_librenms_sync, save_connection,
)
from foxdesk.models import EQUIPMENT_TYPES, DeviceRecord
from foxdesk.services.auth import _log_activity, require_super_admin


@app.route('/admin/librenms', methods=['GET', 'POST'])
@require_super_admin
def admin_librenms():
    row = librenms_settings()
    if request.method == 'POST':
        try:
            save_connection(request.form.get('url'), request.form.get('api_token'), request.form.get('verify_tls') == 'on')
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
            return redirect(url_for('admin_librenms'))
        _, problems = parse_site_rules(request.form.get('site_rules'))
        for p in problems:
            flash(f'Site rules: {p}', 'error')
        row.site_rules = (request.form.get('site_rules') or '').strip() or None
        row.import_types = [t for t in request.form.getlist('import_types') if t in EQUIPMENT_TYPES] or list(EQUIPMENT_TYPES)
        row.auto_add = request.form.get('auto_add') == 'on'
        _log_activity('librenms_edit', 'Saved the LibreNMS connection.')
        db.session.commit()
        if not problems:
            flash('Saved.', 'success')
        return redirect(url_for('admin_librenms'))
    db.session.commit()
    config = librenms_config()
    linked = DeviceRecord.query.filter(DeviceRecord.source == 'librenms', DeviceRecord.registry_id.isnot(None)).count()
    return render_template('admin_librenms.html', row=row, config=config, types=EQUIPMENT_TYPES, linked=linked,
                           summary=describe_summary(row.last_summary) if row.last_summary else None)


@app.route('/admin/librenms/test', methods=['POST'])
@require_super_admin
def admin_librenms_test():
    try:
        count, version = check_connection()
        flash(f'Connected{f" to LibreNMS {version}" if version else ""}: {count} devices.', 'success')
    except LibreNMSError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_librenms'))


@app.route('/admin/librenms/sync', methods=['POST'])
@require_super_admin
def admin_librenms_sync():
    try:
        flash(describe_summary(run_librenms_sync()), 'success')
    except LibreNMSError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_librenms'))
