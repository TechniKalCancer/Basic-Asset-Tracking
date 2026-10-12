"""Pages: spot checks — scan devices around the building with a phone and
confirm each is with the right student."""
import csv
import io
from datetime import timedelta

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for

from foxdesk.automation.engine import emit
from foxdesk.clock import utcnow
from foxdesk.core import app, db
from foxdesk.models import SPOT_CHECK_RESULTS, Person, SpotCheck
from foxdesk.services.auth import _current_actor, _current_site_ids, require_permission
from foxdesk.services.scoping import _scope_people, _sites_for_actor
from foxdesk.services.spot_checks import device_info, find_device, find_person, parse_code, person_info, record_check, resolve


def _scoped_checks():
    site_ids = _current_site_ids()
    query = SpotCheck.query
    if site_ids is not None:
        query = query.filter(SpotCheck.site_id.in_(site_ids or [-1]))
    return query


@app.route('/admin/spot_check')
@require_permission('devices')
def admin_spot_check():
    _, actor_label, _ = _current_actor()
    since = utcnow() - timedelta(hours=12)
    mine = _scoped_checks().filter(SpotCheck.checked_by == actor_label, SpotCheck.created_at >= since)
    open_problems = (_scoped_checks().filter(SpotCheck.result != 'match', SpotCheck.resolved_at.is_(None))
                     .order_by(SpotCheck.created_at.desc()).limit(25).all())
    return render_template('admin_spot_check.html', checked=mine.count(),
                           problems_today=mine.filter(SpotCheck.result != 'match').count(), open_problems=open_problems)


@app.route('/admin/spot_check/lookup')
@require_permission('devices')
def admin_spot_check_lookup():
    """What a scan is: a device (and who should have it), a person (and
    what they should have), or unknown."""
    site_ids = _current_site_ids()
    code = parse_code(request.args.get('code'))
    row = find_device(code, site_ids)
    if row is not None:
        return jsonify(kind='device', **device_info(row))
    person = find_person(code, site_ids)
    if person is not None:
        return jsonify(kind='person', **person_info(person))
    return jsonify(kind='unknown', code=code)


@app.route('/admin/spot_check/people')
@require_permission('devices')
def admin_spot_check_people():
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify(people=[])
    like = f'%{q}%'
    people = (_scope_people(Person.query, _current_site_ids()).filter(Person.is_active.is_(True))
              .filter(db.or_((Person.first_name + ' ' + Person.last_name).ilike(like), Person.email.ilike(like),
                             Person.external_id.ilike(like)))
              .order_by(Person.last_name, Person.first_name).limit(8).all())
    return jsonify(people=[dict(id=p.id, name=p.full_name, grad_year=p.grad_year, role=p.role,
                                site=p.site.name if p.site else None) for p in people])


@app.route('/admin/spot_check/record', methods=['POST'])
@require_permission('devices')
def admin_spot_check_record():
    """Save a check. found_person_id empty = the tech confirmed it's with the
    student FoxDesk expects."""
    site_ids = _current_site_ids()
    data = request.get_json(silent=True) or request.form
    code = parse_code(data.get('code'))
    if not code:
        return jsonify(error='Scan or type a device first.'), 400
    row = find_device(code, site_ids)
    found = None
    if data.get('found_person_id'):
        found = _scope_people(Person.query, site_ids).filter_by(id=int(data['found_person_id'])).first()
        if found is None:
            return jsonify(error='That person isn\'t one you can see.'), 404
    _, actor_label, _ = _current_actor()
    check = record_check(row.asset_tag if row is not None else code, row, found, actor_label, note=data.get('note'))
    if check.result in ('wrong_student', 'unassigned_in_use') and row is not None:
        emit('spotcheck.problem', row, person=check.found, spot_result=check.result)
    return jsonify(id=check.id, result=check.result, label=check.result_label,
                   expected=person_info(check.expected), found=person_info(check.found))


@app.route('/admin/spot_check/<int:check_id>/resolve', methods=['POST'])
@require_permission('devices')
def admin_spot_check_resolve(check_id):
    check = _scoped_checks().filter_by(id=check_id).first() or abort(404)
    _, actor_label, _ = _current_actor()
    resolve(check, actor_label)
    db.session.commit()
    flash(f'{check.asset_tag}: marked resolved.', 'success')
    return redirect(request.referrer or url_for('admin_spot_check'))


def _history_query():
    query = _scoped_checks()
    show = request.args.get('show', 'problems')
    if show == 'problems':
        query = query.filter(SpotCheck.result != 'match', SpotCheck.resolved_at.is_(None))
    elif show in SPOT_CHECK_RESULTS:
        query = query.filter(SpotCheck.result == show)
    days = request.args.get('days', 30, type=int)
    query = query.filter(SpotCheck.created_at >= utcnow() - timedelta(days=max(1, min(days, 400))))
    site_id = request.args.get('site_id', type=int)
    if site_id:
        query = query.filter(SpotCheck.site_id == site_id)
    return query.order_by(SpotCheck.created_at.desc())


@app.route('/admin/spot_check/history')
@require_permission('devices')
def admin_spot_check_history():
    checks = _history_query().limit(500).all()
    totals = dict(_scoped_checks().filter(SpotCheck.created_at >= utcnow() - timedelta(days=request.args.get('days', 30, type=int)))
                  .with_entities(SpotCheck.result, db.func.count(SpotCheck.id)).group_by(SpotCheck.result).all())
    return render_template('admin_spot_check_history.html', checks=checks, totals=totals, results=SPOT_CHECK_RESULTS,
                           show=request.args.get('show', 'problems'), days=request.args.get('days', 30, type=int),
                           sites=_sites_for_actor(_current_site_ids()), site_id=request.args.get('site_id', type=int))


@app.route('/admin/spot_check/history/export')
@require_permission('devices')
def admin_spot_check_export():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['When (UTC)', 'Device', 'Result', 'Should have it', 'Had it', 'School', 'Checked by', 'Note', 'Resolved'])
    for c in _history_query():
        w.writerow([f'{c.created_at:%Y-%m-%d %H:%M}', c.asset_tag, c.result_label, c.expected.full_name if c.expected else '',
                    c.found.full_name if c.found else '', c.site.name if c.site else '', c.checked_by, c.note or '',
                    f'{c.resolved_at:%Y-%m-%d} by {c.resolved_by}' if c.resolved_at else ''])
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="spot-checks.csv"'})
