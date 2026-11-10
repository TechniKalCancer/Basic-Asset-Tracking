"""Pages: end-of-year turn-in sessions (scan into boxes, who's still out,
box lists, and handing devices back in the fall)."""
import csv
import io

from flask import Response, abort, flash, redirect, render_template, request, url_for

from foxdesk.clock import utcnow
from foxdesk.core import app, db
from foxdesk.models import CollectionScan, CollectionSession
from foxdesk.services import collection
from foxdesk.services.auth import _current_actor, _current_site_ids, _log_activity, require_permission
from foxdesk.services.scoping import _sites_for_actor


def _session_or_404(session_id):
    session = db.get_or_404(CollectionSession, session_id)
    site_ids = _current_site_ids()
    if site_ids is not None and session.site_id not in site_ids:
        abort(404)
    return session


@app.route('/admin/turn-in', methods=['GET', 'POST'])
@require_permission('devices')
def admin_turn_in():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:120]
        site_id = request.form.get('site_id', type=int) or None
        if not name:
            flash('Name the turn-in, e.g. "End of year 2027".', 'error')
            return redirect(url_for('admin_turn_in'))
        if site_ids is not None and site_id not in site_ids:
            flash('Pick one of your schools.', 'error')
            return redirect(url_for('admin_turn_in'))
        size = request.form.get('box_size', type=int) or 10
        _, actor_label, _ = _current_actor()
        session = CollectionSession(name=name, site_id=site_id, box_size=min(max(size, 1), 100),
                                    returns_device=bool(request.form.get('returns_device')), created_by=actor_label)
        db.session.add(session)
        _log_activity('device_edit', f'Started turn-in "{name}".', site_id=site_id)
        db.session.commit()
        return redirect(url_for('admin_turn_in_session', session_id=session.id))
    sessions = CollectionSession.query.order_by(CollectionSession.started_at.desc()).all()
    if site_ids is not None:
        sessions = [s for s in sessions if s.site_id in site_ids]
    counts = dict(db.session.query(CollectionScan.session_id, db.func.count(CollectionScan.id)).group_by(CollectionScan.session_id))
    return render_template('admin_turn_in.html', sessions=sessions, counts=counts, sites=_sites_for_actor(site_ids))


@app.route('/admin/turn-in/<int:session_id>', methods=['GET', 'POST'])
@require_permission('devices')
def admin_turn_in_session(session_id):
    session = _session_or_404(session_id)
    mode = request.values.get('mode', 'in')
    if request.method == 'POST':
        _, actor_label, _ = _current_actor()
        value = request.form.get('scan_value', '')
        if mode == 'back':
            status, message, _ = collection.hand_back(session, value, actor_label)
        else:
            status, message, _ = collection.turn_in(session, value, actor_label, request.form.get('note'))
        if status == 'ok':
            db.session.commit()
        else:
            db.session.rollback()
        flash(message, {'ok': 'success', 'already': 'info'}.get(status, 'error'))
        return redirect(url_for('admin_turn_in_session', session_id=session.id, mode=mode))
    if request.args.get('export') == 'outstanding':
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['Student', 'Class of', 'Email', 'Device', 'Serial'])
        for r, p in collection.outstanding(session):
            w.writerow([p.full_name, p.grad_year or '', p.email or '', r.asset_tag, r.serial_number or ''])
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="not-turned-in-{session.id}.csv"'})
    scans = CollectionScan.query.filter_by(session_id=session.id).order_by(CollectionScan.scanned_at.desc()).limit(12).all()
    total = CollectionScan.query.filter_by(session_id=session.id).count()
    handed = CollectionScan.query.filter(CollectionScan.session_id == session.id, CollectionScan.handed_back_at.isnot(None)).count()
    still_out = collection.outstanding(session)
    q = request.args.get('q', '')
    return render_template('admin_turn_in_session.html', turnin=session, mode=mode, recent=scans, total=total, handed=handed,
                           outstanding=still_out, next_slot=collection.next_slot(session), q=q, found=collection.find(session, q))


@app.route('/admin/turn-in/<int:session_id>/boxes')
@require_permission('devices')
def admin_turn_in_boxes(session_id):
    session = _session_or_404(session_id)
    return render_template('admin_turn_in_boxes.html', turnin=session, boxes=collection.boxes(session))


@app.route('/admin/turn-in/<int:session_id>/close', methods=['POST'])
@require_permission('devices')
def admin_turn_in_close(session_id):
    session = _session_or_404(session_id)
    session.closed_at = None if session.closed_at else utcnow()
    _log_activity('device_edit', f'{"Reopened" if session.closed_at is None else "Closed"} turn-in "{session.name}".',
                  site_id=session.site_id)
    db.session.commit()
    flash(f'{session.name} is {"open again" if session.closed_at is None else "closed; hand-back still works"}.', 'success')
    return redirect(url_for('admin_turn_in_session', session_id=session.id))
