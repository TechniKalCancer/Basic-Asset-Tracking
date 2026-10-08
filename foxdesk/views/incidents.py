"""Pages: incidents."""
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from flask import abort, flash, redirect, render_template, request, url_for
from foxdesk.core import app, db
from foxdesk.models import (
    Asset,
    AssetRegistry,
    Attachment,
    BrandingSettings,
    Incident,
    Person,
    Repair,
    RepairCategory,
    Ticket,
    TicketAutomation,
    TicketCharge,
)
from foxdesk.services.util import _parse_money, resolve_scan
from foxdesk.services.auth import (
    _current_actor,
    _current_site_ids,
    _has_permission,
    _log_activity,
    kiosk_or_permission_required,
    require_permission,
)
from foxdesk.services.scoping import _scope_people, _scope_registry, _scope_tickets
from foxdesk.services.incidents import _create_incident, _send_damage_notice
from foxdesk.services.attachments import _attachment_owner_in_scope, _save_attachments


@app.route('/admin/assets/<string:asset_tag>/incidents', methods=['POST'])
@require_permission('devices')
def admin_incident_add(asset_tag):
    """Logs a damage/loss report against an asset, snapshotting the currently
    assigned person. A fee_amount greater than zero forces fee_charged=True
    regardless of the checkbox — an amount implies a charge. Picking a repair
    category is optional (the form JS pre-fills description/fee_amount from
    it, but both stay freely editable, so this route just trusts whatever the
    form actually submitted)."""
    _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    description = request.form.get('description', '').strip()
    fee_charged = request.form.get('fee_charged') == 'on'
    fee_amount = _parse_money(request.form.get('fee_amount'))
    repair_category_id = request.form.get('repair_category_id', type=int)
    if fee_amount:
        fee_charged = True
    if not description:
        flash('Enter a description of the incident.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    person = asset.assigned_to if asset else None
    try:
        incident = _create_incident(asset_tag, person, description, fee_charged=fee_charged, fee_amount=fee_amount,
                                     repair_category_id=repair_category_id)
        db.session.flush()
        _, actor_label, _ = _current_actor()
        _save_attachments('incident', incident.id, request.files.getlist('photos'), uploaded_by=actor_label)
        db.session.commit()
        flash('Incident logged.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not log incident: {e}', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))
    if request.form.get('notify_guardian') == 'on':
        ok, message = _send_damage_notice(incident)
        flash(message, 'success' if ok else 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/incidents/<int:incident_id>/notify_guardian', methods=['POST'])
@require_permission('devices')
def admin_incident_notify_guardian(incident_id):
    incident = _attachment_owner_in_scope('incident', incident_id) or abort(404)
    ok, message = _send_damage_notice(incident)
    flash(message, 'success' if ok else 'error')
    return redirect(request.referrer or url_for('admin_asset_assign', asset_tag=incident.asset_tag))


@app.route('/report_problem', methods=['GET', 'POST'])
@kiosk_or_permission_required('checkinout')
def report_problem_page():
    """
    Student/staff self-service "something's wrong with my device" page — no
    fee fields exposed here, that's an office decision made later from the
    device's assign page. Mirrors loaner_checkout_page's shape: person-search
    + scan input, resolve_scan() with a raw-value fallback, site-check when scoped.
    """
    site_ids = _current_site_ids()
    if request.method == 'POST':
        person_id = request.form.get('person_id', '').strip()
        scan_value = request.form.get('scan_value', '').strip()
        description = request.form.get('description', '').strip()
        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_id)).first() if person_id.isdigit() else None
        if not person or not person.is_active:
            flash('Search for your name and select yourself from the list first.', 'error')
            return redirect(url_for('report_problem_page'))
        if not scan_value:
            flash('Scan or type the device asset tag/serial.', 'error')
            return redirect(url_for('report_problem_page'))
        if not description:
            flash('Describe the problem.', 'error')
            return redirect(url_for('report_problem_page'))

        asset_tag, _ = resolve_scan(scan_value)
        if not asset_tag:
            asset_tag = scan_value  # fall back to raw value, same as loaner_checkin_page
        if site_ids is not None:
            row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
            if not row or row.site_id not in site_ids:
                flash(f'"{scan_value}" was not found in the asset registry.', 'error')
                return redirect(url_for('report_problem_page'))

        try:
            incident = _create_incident(asset_tag, person, description)
            db.session.flush()
            _save_attachments('incident', incident.id, request.files.getlist('photos'), uploaded_by=person.full_name)
            db.session.commit()
            flash('Thanks — your report has been logged.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Could not log report: {e}', 'error')
        return redirect(url_for('report_problem_page'))

    return render_template('report_problem.html')


@app.route('/admin/incidents/<int:incident_id>/mark_paid', methods=['POST'])
@require_permission('devices')
def admin_incident_mark_paid(incident_id):
    incident = Incident.query.get_or_404(incident_id)
    incident.paid_at = datetime.utcnow()
    registry_row = AssetRegistry.query.filter_by(asset_tag=incident.asset_tag).first()
    _log_activity('fee_paid', f'Marked fee paid for {incident.asset_tag} ({incident.person_name or "unknown"}).',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    flash('Marked paid.', 'success')
    return redirect(request.referrer or url_for('admin_asset_assign', asset_tag=incident.asset_tag))


@app.route('/admin/incidents/<int:incident_id>/fee', methods=['POST'])
@require_permission('devices')
def admin_incident_fee(incident_id):
    """Corrects an incident's fee amount after the fact (e.g. the office
    negotiated a lower repair cost than first estimated)."""
    incident = Incident.query.get_or_404(incident_id)
    fee_amount = _parse_money(request.form.get('fee_amount'))
    incident.fee_amount = fee_amount
    incident.fee_charged = bool(fee_amount)
    registry_row = AssetRegistry.query.filter_by(asset_tag=incident.asset_tag).first()
    _log_activity('fee_edit', f'Updated fee for {incident.asset_tag} to {fee_amount if fee_amount else "none"}.',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    flash('Updated fee.', 'success')
    return redirect(request.referrer or url_for('admin_asset_assign', asset_tag=incident.asset_tag))


@app.route('/admin/incidents/<int:incident_id>/delete', methods=['POST'])
@require_permission('devices')
def admin_incident_delete(incident_id):
    """Permanently removes an incident (and its fee, if any) — for
    correcting billing mistakes like a duplicate entry or a charge logged
    against the wrong person. Unlike editing the fee to blank, this drops
    the row entirely, so it also stops counting toward that device's
    incident-escalation history."""
    incident = Incident.query.get_or_404(incident_id)
    asset_tag, person_name, description = incident.asset_tag, incident.person_name, incident.description
    registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
    Attachment.query.filter_by(owner_type='incident', owner_id=incident.id).delete()
    db.session.delete(incident)
    _log_activity('incident_delete', f'Deleted incident on {asset_tag} ({person_name or "unknown"}): {description}',
                   site_id=registry_row.site_id if registry_row else None)
    db.session.commit()
    flash('Incident deleted.', 'success')
    return redirect(request.referrer or url_for('admin_fees'))


@app.route('/admin/fees')
@require_permission('devices')
def admin_fees():
    """Centralized billing — charged incidents AND ticket fees, merged and
    grouped by person, with inline edit/delete/mark-paid on every line so
    the office never has to hunt down the originating device or ticket to
    fix a mistake. Ticket fees are only folded in when the viewer also
    holds the 'tickets' permission — otherwise the links on those rows
    would 403 for them. status defaults to 'unpaid' (the original "who
    owes" view); 'paid'/'all' add billing history."""
    site_ids = _current_site_ids()
    status = request.args.get('status', 'unpaid').strip()
    if status not in ('unpaid', 'paid', 'all'):
        status = 'unpaid'
    search = request.args.get('q', '').strip()

    inc_query = Incident.query.filter(Incident.fee_charged.is_(True))
    if status == 'unpaid':
        inc_query = inc_query.filter(Incident.paid_at.is_(None))
    elif status == 'paid':
        inc_query = inc_query.filter(Incident.paid_at.isnot(None))
    if site_ids is not None:
        inc_query = inc_query.join(AssetRegistry, AssetRegistry.asset_tag == Incident.asset_tag) \
            .filter(AssetRegistry.site_id.in_(site_ids))
    if search:
        like = f'%{search}%'
        inc_query = inc_query.filter(db.or_(
            Incident.person_name.ilike(like), Incident.description.ilike(like), Incident.asset_tag.ilike(like)))
    incidents = inc_query.order_by(Incident.person_name, Incident.created_at).all()

    by_person = OrderedDict()
    grand_total = Decimal('0')
    for inc in incidents:
        key = inc.person_name or '(no person on file)'
        entry = by_person.setdefault(key, {'charges': [], 'subtotal': Decimal('0')})
        amount = inc.fee_amount or Decimal('0')
        entry['charges'].append({
            'date': inc.created_at, 'label': f'Incident: {inc.description}', 'amount': amount,
            'paid': inc.paid_at is not None, 'is_ticket_charge': False,
            'mark_paid_url': url_for('admin_incident_mark_paid', incident_id=inc.id),
            'fee_url': url_for('admin_incident_fee', incident_id=inc.id),
            'delete_url': url_for('admin_incident_delete', incident_id=inc.id),
            'delete_confirm': f'Delete this incident ({inc.asset_tag}, ${amount:.2f})? This removes the incident '
                               'record entirely (not just the charge) — it will no longer count toward that '
                               'device\'s incident history.',
            'link_url': url_for('admin_asset_assign', asset_tag=inc.asset_tag), 'link_label': inc.asset_tag,
        })
        entry['subtotal'] += amount
        grand_total += amount

    if _has_permission('tickets'):
        tc_query = _scope_tickets(TicketCharge.query.join(Ticket, Ticket.id == TicketCharge.ticket_id), site_ids)
        if status == 'unpaid':
            tc_query = tc_query.filter(TicketCharge.paid_at.is_(None))
        elif status == 'paid':
            tc_query = tc_query.filter(TicketCharge.paid_at.isnot(None))
        if search:
            like = f'%{search}%'
            tc_query = tc_query.filter(db.or_(
                Ticket.requester_name.ilike(like), Ticket.subject.ilike(like), TicketCharge.description.ilike(like)))
        charges = tc_query.order_by(Ticket.requester_name, TicketCharge.created_at).all()
        for tc in charges:
            t = tc.ticket
            key = t.requester_name or '(no person on file)'
            entry = by_person.setdefault(key, {'charges': [], 'subtotal': Decimal('0')})
            amount = tc.amount or Decimal('0')
            entry['charges'].append({
                'date': tc.created_at, 'label': f'Ticket #{t.id}: {tc.description}', 'amount': amount,
                'paid': tc.paid_at is not None, 'is_ticket_charge': True, 'description': tc.description,
                'mark_paid_url': url_for('admin_ticket_charge_mark_paid', charge_id=tc.id),
                'fee_url': url_for('admin_ticket_charge_edit', charge_id=tc.id),
                'delete_url': url_for('admin_ticket_charge_delete', charge_id=tc.id),
                'delete_confirm': f'Delete this ${amount:.2f} charge from ticket #{t.id}? This cannot be undone.',
                'link_url': url_for('admin_ticket_detail', ticket_id=t.id), 'link_label': f'#{t.id}',
            })
            entry['subtotal'] += amount
            grand_total += amount

    for entry in by_person.values():
        entry['charges'].sort(key=lambda c: c['date'])
    by_person = OrderedDict(sorted(by_person.items(), key=lambda kv: kv[0]))

    return render_template('admin_fees.html', by_person=by_person, grand_total=grand_total, status=status)


@app.route('/admin/assets/<string:asset_tag>/invoice')
@require_permission('devices')
def admin_asset_invoice(asset_tag):
    """Printable invoice listing every incident logged against this device —
    the "list of damages" comes from however many separate Incident rows
    exist for this asset_tag, each optionally tagged with a RepairCategory;
    no separate line-item table needed since Incident is already a per-asset
    list. Browser print (window.print()), same as any other print-friendly
    page in this app — no PDF library involved."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    incidents = Incident.query.filter_by(asset_tag=asset_tag).order_by(Incident.created_at).all()
    total = sum((inc.fee_amount or Decimal('0')) for inc in incidents if inc.fee_charged)
    return render_template('admin_asset_invoice.html', registry_row=registry_row, incidents=incidents,
                           total=total, branding_settings=BrandingSettings.query.get(1),
                           generated_at=datetime.utcnow())


@app.route('/admin/repair_categories')
@require_permission('devices')
def admin_repair_categories():
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = RepairCategory.query
    if search:
        query = query.filter(RepairCategory.name.ilike(f'%{search}%'))
    categories = query.order_by(RepairCategory.name.desc() if sort_dir == 'desc' else RepairCategory.name.asc()).all()
    return render_template('admin_repair_categories.html', categories=categories, search=search, sort_dir=sort_dir)


@app.route('/admin/repair_categories/new', methods=['GET', 'POST'])
@require_permission('devices')
def admin_repair_category_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_repair_category_form.html', category=None, form=request.form)
        if RepairCategory.query.filter(db.func.lower(RepairCategory.name) == name.lower()).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_repair_category_form.html', category=None, form=request.form)

        db.session.add(RepairCategory(name=name, default_price=default_price))
        _log_activity('repair_category_add', f'Added repair category "{name}".')
        db.session.commit()
        flash(f'Added category "{name}".', 'success')
        return redirect(url_for('admin_repair_categories'))

    return render_template('admin_repair_category_form.html', category=None, form=None)


@app.route('/admin/repair_categories/<int:category_id>/edit', methods=['GET', 'POST'])
@require_permission('devices')
def admin_repair_category_edit(category_id):
    category = RepairCategory.query.get_or_404(category_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        default_price = _parse_money(request.form.get('default_price'))
        is_active = bool(request.form.get('is_active'))
        if not name:
            flash('Name is required.', 'error')
            return render_template('admin_repair_category_form.html', category=category, form=None)
        if RepairCategory.query.filter(db.func.lower(RepairCategory.name) == name.lower(),
                                        RepairCategory.id != category_id).first():
            flash(f'A category named "{name}" already exists.', 'error')
            return render_template('admin_repair_category_form.html', category=category, form=None)

        category.name = name
        category.default_price = default_price
        category.is_active = is_active
        _log_activity('repair_category_edit', f'Edited repair category "{name}".')
        db.session.commit()
        flash(f'Updated category "{name}".', 'success')
        return redirect(url_for('admin_repair_categories'))

    return render_template('admin_repair_category_form.html', category=category, form=None)


@app.route('/admin/repair_categories/<int:category_id>/delete', methods=['POST'])
@require_permission('devices')
def admin_repair_category_delete(category_id):
    category = RepairCategory.query.get_or_404(category_id)
    # Both Incident and Repair reuse this same category catalog (see
    # RepairCategory's docstring) — checking only one would let the other's
    # reference through to a ForeignKeyViolation on delete.
    incident_count = Incident.query.filter_by(repair_category_id=category_id).count()
    repair_count = Repair.query.filter_by(repair_category_id=category_id).count()
    automation_count = TicketAutomation.query.filter_by(repair_category_id=category_id).count()
    if incident_count or repair_count or automation_count:
        parts = []
        if incident_count:
            parts.append(f'{incident_count} incident(s)')
        if repair_count:
            parts.append(f'{repair_count} repair(s)')
        if automation_count:
            parts.append(f'{automation_count} automation(s)')
        flash(f'Cannot delete "{category.name}" — {" and ".join(parts)} still reference it. Deactivate it instead.', 'error')
        return redirect(url_for('admin_repair_categories'))
    name = category.name
    db.session.delete(category)
    _log_activity('repair_category_delete', f'Deleted repair category "{name}".')
    db.session.commit()
    flash(f'Deleted category "{name}".', 'success')
    return redirect(url_for('admin_repair_categories'))
