"""Pages: SLA targets (Settings → SLA targets)."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.clock import utcnow
from foxdesk.core import APP_TIMEZONE, app, db
from foxdesk.models import TICKET_PRIORITIES
from foxdesk.services.auth import _log_activity, require_super_admin
from foxdesk.services.sla import (
    WEEKDAY_NAMES, Calendar, parse_closed_dates, parse_hhmm, reapply_open_tickets, sla_settings, target_hours,
    when_label,
)


def _hours(value):
    """A target from the form: hours as a number, or None for blank."""
    value = (value or '').strip()
    if not value:
        return None, None
    try:
        hours = round(float(value), 2)
    except ValueError:
        return None, f'"{value}" isn\'t a number of hours.'
    if hours <= 0 or hours > 2000:
        return None, f'{value} hours is out of range.'
    return hours, None


@app.route('/admin/sla', methods=['GET', 'POST'])
@require_super_admin
def admin_sla():
    row = sla_settings()
    if request.method == 'POST':
        problems = []
        days = sorted({int(d) for d in request.form.getlist('work_days') if d.isdigit() and 0 <= int(d) <= 6})
        start, end = parse_hhmm(request.form.get('day_start')), parse_hhmm(request.form.get('day_end'))
        if not days:
            problems.append('Pick at least one working day.')
        if not start or not end or end <= start:
            problems.append('The school day has to end after it starts.')
        _, bad_dates = parse_closed_dates(request.form.get('closed_dates'))
        problems += [f'Closed dates: couldn\'t read "{line}". Use 2026-11-26 or 2026-12-21 to 2027-01-01.' for line in bad_dates]
        targets = {}
        for p in TICKET_PRIORITIES:
            targets[p] = {}
            for kind in ('response', 'resolve'):
                hours, problem = _hours(request.form.get(f'{p}_{kind}'))
                if problem:
                    problems.append(f'{p.capitalize()}: {problem}')
                targets[p][kind] = hours
            if targets[p]['response'] and targets[p]['resolve'] and targets[p]['resolve'] < targets[p]['response']:
                problems.append(f'{p.capitalize()}: the fix time can\'t be shorter than the reply time.')
        if problems:
            for problem in problems:
                flash(problem, 'error')
            return redirect(url_for('admin_sla'))
        row = sla_settings(create=True)
        turning_on = request.form.get('enabled') == 'on' and not row.enabled
        row.enabled = request.form.get('enabled') == 'on'
        if turning_on:
            row.started_at = utcnow()  # clocks start with the next ticket, not on the ones already open
        row.work_days = ','.join(str(d) for d in days)
        row.day_start, row.day_end = start.strftime('%H:%M'), end.strftime('%H:%M')
        row.closed_dates = (request.form.get('closed_dates') or '').strip()
        row.targets = targets
        db.session.flush()
        count = reapply_open_tickets()
        _log_activity('sla_edit', f'SLA targets {"turned on" if row.enabled else "saved (off)"}; due times updated on '
                                  f'{count} open ticket(s).')
        db.session.commit()
        if turning_on:
            flash('SLAs are on. Clocks start with the next ticket opened; tickets already open don\'t get one.', 'success')
        else:
            flash(f'Saved. Due times updated on {count} open ticket(s).' if row.enabled else 'Saved. SLAs are off.', 'success')
        return redirect(url_for('admin_sla'))

    cal = Calendar(row)
    now = utcnow()
    preview = [(p, [when_label(cal.add_hours(now, target_hours(row, p, k)), cal) if target_hours(row, p, k) else '—'
                    for k in ('response', 'resolve')]) for p in TICKET_PRIORITIES]
    return render_template('admin_sla.html', row=row, priorities=TICKET_PRIORITIES, weekdays=WEEKDAY_NAMES,
                           work_days={int(d) for d in (row.work_days or '').split(',') if d.isdigit()},
                           preview=preview, timezone=APP_TIMEZONE)
