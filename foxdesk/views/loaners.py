"""Pages: loaners."""
from datetime import datetime
from flask import flash, jsonify, redirect, render_template, request, url_for
from foxdesk.core import EMAIL_ENABLED, app, db, logger
from foxdesk.models import Asset, AssetRegistry, LoanerCheckout, Person, Site
from foxdesk.services.util import resolve_scan
from foxdesk.services.emailer import send_email
from foxdesk.services.auth import (
    _current_site_ids,
    _has_permission,
    _log_activity,
    kiosk_or_api_permission_required,
    kiosk_or_permission_required,
    login_required,
    require_permission,
)
from foxdesk.services.scoping import _filter_registry_by_status, _scope_people, _scope_registry
from foxdesk.services.assignments import (
    _checkin_loaner,
    _checkout_loaner,
    _loaner_reminder_email_content,
    _overdue_loaners,
    _send_overdue_loaner_reminders,
)


@app.route('/admin/assets/<string:asset_tag>/toggle_loaner', methods=['POST'])
@require_permission('devices_manage')
def admin_toggle_loaner(asset_tag):
    row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    if not row.is_loaner:
        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if asset and asset.assigned_to_id:
            flash(f'{asset_tag} is currently assigned to {asset.assigned_to.full_name} — unassign it first before marking it a loaner.', 'error')
            return redirect(request.referrer or url_for('admin_loaners'))
    row.is_loaner = not row.is_loaner
    loaner_label = request.form.get('loaner_label', '').strip()
    if loaner_label:
        row.loaner_label = loaner_label
    label_note = f' Labeled "{row.loaner_label}".' if row.is_loaner and row.loaner_label else ''
    _log_activity('loaner_toggle', f'{asset_tag} is {"now" if row.is_loaner else "no longer"} in the loaner pool.{label_note}', site_id=row.site_id)
    db.session.commit()
    flash(f'{asset_tag} is {"now" if row.is_loaner else "no longer"} in the loaner pool.', 'success')
    return redirect(request.referrer or url_for('admin_loaners'))


@app.route('/admin/assets/<string:asset_tag>/loaner_label', methods=['POST'])
@require_permission('devices_manage')
def admin_update_loaner_label(asset_tag):
    """Edits a loaner's tracking label without touching is_loaner — for
    relabeling a device that's already in the pool (the toggle route above
    only saves a label at the moment of marking something a loaner)."""
    row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    row.loaner_label = request.form.get('loaner_label', '').strip() or None
    _log_activity('loaner_label_edit', f'Set loaner label for {asset_tag}: "{row.loaner_label or ""}".', site_id=row.site_id)
    db.session.commit()
    flash('Loaner label updated.', 'success')
    return redirect(request.referrer or url_for('admin_asset_assign', asset_tag=asset_tag))


LOANER_POOL_SORT_COLUMNS = {
    'asset_tag': (AssetRegistry.asset_tag,),
    'serial_number': (AssetRegistry.serial_number,),
    'description': (AssetRegistry.description,),
    'site': (Site.name,),
    'due_date': (LoanerCheckout.due_date,),
}


@app.route('/admin/loaners')
@require_permission('loaners')
def admin_loaners():
    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'asset_tag').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in LOANER_POOL_SORT_COLUMNS and sort != 'status':
        sort = 'asset_tag'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    today = datetime.utcnow().date()

    query = _scope_registry(AssetRegistry.query, site_ids).filter_by(is_loaner=True)
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(
            AssetRegistry.asset_tag.ilike(like), AssetRegistry.serial_number.ilike(like),
            AssetRegistry.description.ilike(like),
        ))
    if sort == 'site':
        query = query.outerjoin(Site, AssetRegistry.site_id == Site.id)
    elif sort in ('due_date', 'status'):
        # Status/due-date live on the open LoanerCheckout, not AssetRegistry
        # itself, so those two sorts need the checkout joined in first.
        query = query.outerjoin(LoanerCheckout, db.and_(
            LoanerCheckout.asset_tag == AssetRegistry.asset_tag, LoanerCheckout.checked_in_at.is_(None)))

    if sort == 'status':
        # Available (0) < Checked Out, not yet due (1) < Overdue (2).
        status_rank = db.case(
            (LoanerCheckout.id.is_(None), 0),
            (LoanerCheckout.due_date < today, 2),
            else_=1,
        )
        order_exprs = [status_rank.desc() if sort_dir == 'desc' else status_rank.asc()]
    else:
        sort_cols = LOANER_POOL_SORT_COLUMNS[sort]
        order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in sort_cols]
    loaner_rows = query.order_by(*order_exprs, AssetRegistry.asset_tag).all()

    tags = [r.asset_tag for r in loaner_rows]
    open_checkouts = {
        c.asset_tag: c for c in LoanerCheckout.query.filter(
            LoanerCheckout.asset_tag.in_(tags), LoanerCheckout.checked_in_at.is_(None)
        )
    }
    overdue_count = len(_overdue_loaners(site_ids))
    return render_template('admin_loaners.html', loaner_rows=loaner_rows, open_checkouts=open_checkouts,
                           overdue_count=overdue_count, email_enabled=EMAIL_ENABLED,
                           today=datetime.utcnow().date(), search=search, sort=sort, sort_dir=sort_dir)


@app.route('/admin/collection')
@login_required
def admin_collection():
    """
    "Who still has what" — currently-assigned devices unioned with currently-
    checked-out loaners, site-scoped, in one combined view. Useful for
    end-of-year collection: instead of cross-referencing the registry and
    loaner pages separately, see the whole outstanding list at once.

    Gated like admin_panel.html's dashboard cards — each half only shows for
    a session with that specific permission, rather than requiring both
    'devices' and 'loaners' just to see either one.
    """
    if not (_has_permission('devices') or _has_permission('loaners')):
        flash('Your account doesn\'t have permission to access that page.', 'error')
        return redirect(url_for('admin_panel'))

    site_ids = _current_site_ids()
    search = request.args.get('q', '').strip()

    assigned_rows = []
    if _has_permission('devices'):
        registry_rows = _filter_registry_by_status(
            _scope_registry(AssetRegistry.query, site_ids), 'assigned'
        ).order_by(AssetRegistry.asset_tag).all()
        tags = [r.asset_tag for r in registry_rows]
        assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(tags))}
        assigned_rows = [(r, assets_by_tag.get(r.asset_tag)) for r in registry_rows]
        if search:
            needle = search.lower()
            assigned_rows = [
                (r, a) for r, a in assigned_rows
                if needle in (r.asset_tag or '').lower() or needle in (r.description or '').lower()
                or (a and a.assigned_to and needle in a.assigned_to.full_name.lower())
            ]

    open_loaners = []
    if _has_permission('loaners'):
        loaner_query = LoanerCheckout.query.filter(LoanerCheckout.checked_in_at.is_(None))
        if site_ids is not None:
            loaner_query = loaner_query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
                .filter(AssetRegistry.site_id.in_(site_ids))
        if search:
            like = f'%{search}%'
            loaner_query = loaner_query.filter(db.or_(
                LoanerCheckout.asset_tag.ilike(like), LoanerCheckout.person_name.ilike(like),
            ))
        open_loaners = loaner_query.order_by(LoanerCheckout.checked_out_at).all()

    return render_template('admin_collection.html', assigned_rows=assigned_rows, open_loaners=open_loaners,
                           now=datetime.utcnow().date(), search=search)


@app.route('/admin/loaners/send_reminders', methods=['POST'])
@require_permission('loaners')
def admin_loaners_send_reminders():
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_FROM_EMAIL (and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth) in .env to enable it.', 'info')
        return redirect(url_for('admin_loaners'))
    sent, failed, skipped = _send_overdue_loaner_reminders(_current_site_ids())
    msg = f'Sent {sent} reminder{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'info')
    return redirect(url_for('admin_loaners'))


@app.route('/admin/loaners/email_selected', methods=['POST'])
@require_permission('loaners')
def admin_loaners_email_selected():
    """Emails a hand-picked set of currently-checked-out loaners, whether
    overdue or not — unlike admin_loaners_send_reminders (which blankets
    every overdue loaner), this lets the office remind someone their loaner
    is coming due soon, not just chase people who are already late. Ignores
    the reminder_sent_at resend gate since this is an explicit one-off send,
    not the automatic hourly sweep."""
    if not EMAIL_ENABLED:
        flash('Email isn\'t configured yet. Set SMTP_FROM_EMAIL (and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth) in .env to enable it.', 'info')
        return redirect(url_for('admin_loaners'))
    site_ids = _current_site_ids()
    asset_tags = request.form.getlist('asset_tags')
    if not asset_tags:
        flash('Select at least one checked-out loaner to email.', 'error')
        return redirect(url_for('admin_loaners'))

    query = LoanerCheckout.query.filter(
        LoanerCheckout.asset_tag.in_(asset_tags), LoanerCheckout.checked_in_at.is_(None))
    if site_ids is not None:
        query = query.join(AssetRegistry, AssetRegistry.asset_tag == LoanerCheckout.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    rows = query.all()

    now = datetime.utcnow()
    sent = failed = skipped = 0
    for row in rows:
        person = Person.query.get(row.person_id) if row.person_id else None
        if not person:
            skipped += 1
            continue
        subject, body = _loaner_reminder_email_content(row, person, now)
        try:
            send_email(person.email, subject, body)
            row.reminder_sent_at = now
            sent += 1
        except Exception as e:
            failed += 1
            logger.error('Loaner reminder email failed for %s -> %s: %s', row.asset_tag, person.email, e)

    if sent:
        _log_activity('reminders_send', f'Manually emailed {sent} selected loaner(s).')
    db.session.commit()
    msg = f'Emailed {sent} selected loaner{"s" if sent != 1 else ""}.'
    if failed:
        msg += f' {failed} failed to send.'
    if skipped:
        msg += f' {skipped} skipped (person no longer exists).'
    flash(msg, 'success' if sent else 'info')
    return redirect(url_for('admin_loaners'))


@app.route('/admin/loaners/checkout', methods=['POST'])
@require_permission('loaners')
def admin_loaners_checkout():
    site_ids = _current_site_ids()
    scan_value = request.form.get('asset_tag', '').strip()
    person_id = request.form.get('person_id', '').strip()
    due_date_str = request.form.get('due_date', '').strip()
    acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
    person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
    if not scan_value or not person:
        flash('Choose both a person and a loaner asset tag.', 'error')
        return redirect(url_for('admin_loaners'))
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        flash(f'"{scan_value}" was not found in the asset registry.', 'error')
        return redirect(url_for('admin_loaners'))
    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid due date.', 'error')
            return redirect(url_for('admin_loaners'))
    status, message = _checkout_loaner(asset_tag, person, due_date, site_ids, acknowledged_by=acknowledged_by)
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(url_for('admin_loaners'))


@app.route('/admin/loaners/checkin', methods=['POST'])
@require_permission('loaners')
def admin_loaners_checkin():
    asset_tag = request.form.get('asset_tag', '').strip()
    status, message = _checkin_loaner(asset_tag, site_ids=_current_site_ids())
    flash(message, 'success' if status == 'ok' else 'error')
    return redirect(request.referrer or url_for('admin_loaners'))


@app.route('/loaner_checkout', methods=['GET', 'POST'])
@kiosk_or_permission_required('loaner_checkinout')
def loaner_checkout_page():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        scan_value = request.form.get('scan_value', '').strip()
        acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        if not acknowledged_by:
            flash('Type your name to acknowledge responsibility for this device.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            flash(f'"{scan_value}" was not found in the asset registry.', 'error')
            return redirect(url_for('loaner_checkout_page'))
        status, message = _checkout_loaner(asset_tag, person, site_ids=site_ids, acknowledged_by=acknowledged_by)
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkout_page'))

    return render_template('loaner_checkout.html')


@app.route('/loaner_checkin', methods=['GET', 'POST'])
@kiosk_or_permission_required('loaner_checkinout')
def loaner_checkin_page():
    if request.method == 'POST':
        scan_value = request.form.get('scan_value', '').strip()
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkin_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value so a direct tag match on LoanerCheckout still works
        status, message = _checkin_loaner(asset_tag, site_ids=_current_site_ids())
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkin_page'))

    return render_template('loaner_checkin.html')


@app.route('/api/loaner_lookup')
@kiosk_or_api_permission_required('loaner_checkinout')
def api_loaner_lookup():
    """
    Backs the merged /loaner_checkinout page's scan-and-detect flow: given a
    scanned tag/serial, tells the client whether this will be a checkout
    (device available) or a checkin (device currently out, and to whom) —
    without the client needing to know the difference up front. Site-scoped
    the same way the checkout/checkin routes themselves are, so a site-scoped
    kiosk/user can't learn another site's loaner status through this endpoint.
    """
    scan_value = request.args.get('scan_value', '').strip()
    if not scan_value:
        return jsonify({'found': False})
    asset_tag, _ = resolve_scan(scan_value)
    if not asset_tag:
        return jsonify({'found': False})
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    site_ids = _current_site_ids()
    if not row or not row.is_loaner or (site_ids is not None and row.site_id not in site_ids):
        return jsonify({'found': True, 'asset_tag': asset_tag, 'is_loaner': False})
    open_checkout = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
    return jsonify({
        'found': True, 'asset_tag': asset_tag, 'is_loaner': True,
        'checked_out': bool(open_checkout),
        'checked_out_to': open_checkout.person_name if open_checkout else None,
    })


@app.route('/loaner_checkinout', methods=['GET', 'POST'])
@kiosk_or_permission_required('loaner_checkinout')
def loaner_checkinout_page():
    """
    Merged self-service loaner page — one scan decides checkout vs checkin
    (see api_loaner_lookup for the client-side detection), so the "average
    user" doesn't have to know or choose which of two pages they need.
    Re-derives the mode itself from current DB state rather than trusting
    anything the client sent, same as every other mutating route here.
    """
    site_ids = _current_site_ids()
    if request.method == 'POST':
        scan_value = request.form.get('scan_value', '').strip()
        if not scan_value:
            flash('Scan or type the loaner asset tag/serial.', 'error')
            return redirect(url_for('loaner_checkinout_page'))
        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value, same as loaner_checkin_page

        open_checkout = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
        if open_checkout:
            status, message = _checkin_loaner(asset_tag, site_ids=site_ids)
        else:
            person_id = request.form.get('person_id', '').strip()
            acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
            person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
            if not person or not person.is_active:
                flash('Search for your name and select yourself from the list first.', 'error')
                return redirect(url_for('loaner_checkinout_page'))
            if not acknowledged_by:
                flash('Type your name to acknowledge responsibility for this device.', 'error')
                return redirect(url_for('loaner_checkinout_page'))
            status, message = _checkout_loaner(asset_tag, person, site_ids=site_ids, acknowledged_by=acknowledged_by)
        flash(message, 'success' if status == 'ok' else 'error')
        return redirect(url_for('loaner_checkinout_page'))

    return render_template('loaner_checkinout.html')
