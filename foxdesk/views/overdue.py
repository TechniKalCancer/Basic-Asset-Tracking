"""Pages: what's overdue at each school, the daily tech email's settings, and
the student schedule import."""
from flask import flash, redirect, render_template, request, url_for
from foxdesk.core import EMAIL_ENABLED, app, db
from foxdesk.models import Site, StudentSchedule
from foxdesk.services.auth import _current_site_ids, _current_user, _has_permission, _log_activity, require_permission
from foxdesk.services.overdue import (
    DEFAULT_REPAIR_MAX_DAYS, digest_text, overdue_by_site, overdue_settings, send_tech_alerts,
)
from foxdesk.services.schedules import import_schedules

MAX_SCHEDULE_UPLOAD = 15 * 1024 * 1024


@app.route('/admin/overdue')
@require_permission('repairs')
def admin_overdue():
    """Every school's overdue repairs, loaners and due-back devices — the same
    list each school's techs get by email on school-day mornings."""
    by_site = overdue_by_site(_current_site_ids())
    sites = Site.query.order_by(Site.name).all()
    me = _current_user()
    preview = None
    if me and request.args.get('preview'):
        mine = [s for sid, s in overdue_by_site().items() if s['site'] and me in s['site'].techs]
        preview = digest_text(mine)[1] if mine else ''
    return render_template('admin_overdue.html', by_site=by_site, settings=overdue_settings(), sites=sites,
                           email_enabled=EMAIL_ENABLED, me=me, preview=preview, default_max=DEFAULT_REPAIR_MAX_DAYS,
                           can_admin=_has_permission('admin'))


@app.route('/admin/overdue/settings', methods=['POST'])
@require_permission('admin')
def admin_overdue_settings():
    row = overdue_settings(create=True)
    try:
        max_days = int(request.form.get('repair_max_days', ''))
        hour = int(request.form.get('alert_hour', ''))
    except ValueError:
        flash('Enter a number of days and an hour.', 'error')
        return redirect(url_for('admin_overdue'))
    if not 1 <= max_days <= 120 or not 0 <= hour <= 23:
        flash('Repairs can be limited to 1–120 days, and the email hour is 0–23.', 'error')
        return redirect(url_for('admin_overdue'))
    row.repair_max_days, row.alert_hour = max_days, hour
    row.alerts_enabled = bool(request.form.get('alerts_enabled'))
    _log_activity('settings_edit', f'Overdue settings: repairs {max_days} days max, tech email '
                  f'{"on at " + str(hour) + ":00" if row.alerts_enabled else "off"}.')
    db.session.commit()
    flash('Saved.', 'success')
    return redirect(url_for('admin_overdue'))


@app.route('/admin/overdue/send_mine', methods=['POST'])
@require_permission('repairs')
def admin_overdue_send_mine():
    """Emails the signed-in tech their own list now, to see what it looks like."""
    me = _current_user()
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured on this server.', 'error')
    elif not me or not me.email:
        flash('Your user account needs an email address first (Users & Permissions).', 'error')
    else:
        sent, failed = send_tech_alerts(only_user=me)
        if sent:
            flash(f'Sent to {me.email}.', 'success')
        elif failed:
            flash('The email didn\'t send; check the mail settings.', 'error')
        else:
            flash('Nothing is overdue at your schools, so there\'s nothing to send. '
                  'You\'re only emailed for schools where you\'re a tech (Admin → Sites).', 'info')
    return redirect(url_for('admin_overdue'))


@app.route('/admin/people/schedules', methods=['GET', 'POST'])
@require_permission('people')
def admin_schedules_import():
    """Student schedules from the SIS: upload, see what matched, done."""
    result = None
    if request.method == 'POST':
        upload = request.files.get('file')
        if not upload or not upload.filename:
            flash('Choose the schedule CSV first.', 'error')
            return redirect(url_for('admin_schedules_import'))
        if not upload.filename.lower().endswith(('.csv', '.txt', '.tsv')):
            flash('Save the export as CSV first (in Excel: File → Save As → CSV).', 'error')
            return redirect(url_for('admin_schedules_import'))
        data = upload.read(MAX_SCHEDULE_UPLOAD + 1)
        if len(data) > MAX_SCHEDULE_UPLOAD:
            flash('That file is over 15 MB. Split it by grade.', 'error')
            return redirect(url_for('admin_schedules_import'))
        try:
            result = import_schedules(data)
            if result.get('error'):
                db.session.rollback()
                flash(result['error'], 'error')
                result = None
            else:
                _log_activity('people_import', f'Imported schedules: {result["classes"]} classes for '
                              f'{result["students"]} students ({len(result["unmatched"])} rows not matched).')
                db.session.commit()
                flash(f'Imported {result["classes"]} classes for {result["students"]} students.',
                      'success' if result['students'] else 'error')
        except Exception as e:
            db.session.rollback()
            flash(f'Could not import that file: {e}', 'error')
            result = None
    total = db.session.query(db.func.count(db.distinct(StudentSchedule.person_id))).scalar()
    last = db.session.query(db.func.max(StudentSchedule.imported_at)).scalar()
    return render_template('admin_schedules_import.html', result=result, total=total, last=last,
                           sites=Site.query.order_by(Site.name).all())
