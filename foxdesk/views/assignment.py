"""Pages: assignment."""
import csv
import io
from datetime import datetime
from flask import abort, flash, redirect, render_template, request, url_for
from foxdesk.core import EMAIL_ENABLED, GOOGLE_SYNC_ENABLED, app, db
from foxdesk.automation.engine import emit
from foxdesk.models import (
    ASSET_STATUSES,
    AUTOMATION_ACTIONS,
    Asset,
    AssetRegistry,
    AssignmentHistory,
    AuditScan,
    CustomField,
    DEVICE_TYPES,
    Event,
    Incident,
    LoanerCheckout,
    PendingDeviceAction,
    Person,
    REPAIR_OUTCOMES,
    Repair,
    RepairCategory,
)
from foxdesk.services.auth import _current_site_ids, _log_activity, require_permission
from foxdesk.services.scoping import _scope_people, _scope_registry
from foxdesk.services.assignments import _assign_asset_to_person, _close_open_assignment
from foxdesk.services.labels import AVERY_TEMPLATES, code128_svg
from foxdesk.services.attachments import _attachments_for
from foxdesk.services.reports import _signin_mismatches


@app.route('/admin/assets/<string:asset_tag>/assign', methods=['GET', 'POST'])
@require_permission('devices')
def admin_asset_assign(asset_tag):
    """
    Assigns a person to an asset_tag. The asset_tag must exist in the registry;
    the live Asset row is created on first assignment if scanning hasn't made one yet.
    Reassigning to someone new closes out the prior AssignmentHistory row and opens
    a new one; reassigning to the same person is a no-op.
    """
    site_ids = _current_site_ids()
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first_or_404()
    asset = Asset.query.filter_by(asset_tag=asset_tag).first()

    if request.method == 'POST':
        person_id = request.form.get('person_id', type=int)
        condition_out = request.form.get('condition_out', '').strip() or None
        due_date_str = request.form.get('due_date', '').strip()
        acknowledged_by = request.form.get('acknowledged_by', '').strip() or None
        if not person_id:
            flash('Select a person to assign.', 'error')
            return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid due date.', 'error')
                return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

        person = _scope_people(Person.query, site_ids).filter_by(id=person_id).first_or_404()
        status, message = _assign_asset_to_person(asset_tag, person, condition_out, due_date,
                                                    acknowledged_by=acknowledged_by)
        if status == 'assigned':
            emit('device.assigned', registry_row, person=person)
        if status == 'assigned' and registry_row.site_id and person.site_id and registry_row.site_id != person.site_id:
            message += ' Note: this device and person are at different sites.'
        flash(message, 'info' if status == 'already' else ('success' if status == 'assigned' else 'error'))
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    has_people = Person.query.first() is not None
    history = AssignmentHistory.query.filter_by(asset_tag=asset_tag) \
        .order_by(AssignmentHistory.assigned_at.desc()).all()
    loaner_checkouts = LoanerCheckout.query.filter_by(asset_tag=asset_tag) \
        .order_by(LoanerCheckout.checked_out_at.desc()).all()
    # Normalized into a common shape and interleaved chronologically — an
    # asset that's ever spent time in the loaner pool otherwise had an
    # incomplete history here (assign-only), even though its loaner checkouts
    # are tracked in a separate table.
    combined_history = sorted(
        [{
            'kind': 'assign', 'person_name': h.person_name, 'started_at': h.assigned_at,
            'ended_at': h.unassigned_at, 'due_date': h.due_date, 'acknowledged_by': h.acknowledged_by,
            'notes': ' '.join(filter(None, [
                f'Out: {h.condition_out}' if h.condition_out else None,
                f'In: {h.condition_in}' if h.condition_in else None,
            ])) or None,
        } for h in history] +
        [{
            'kind': 'loaner', 'person_name': l.person_name, 'started_at': l.checked_out_at,
            'ended_at': l.checked_in_at, 'due_date': l.due_date, 'acknowledged_by': l.acknowledged_by,
            'notes': l.condition_notes,
        } for l in loaner_checkouts],
        key=lambda row: row['started_at'], reverse=True,
    )
    events = Event.query.filter_by(asset_tag=asset_tag) \
        .order_by(Event.timestamp.desc()).limit(20).all()
    incidents = Incident.query.filter_by(asset_tag=asset_tag) \
        .order_by(Incident.created_at.desc()).all()

    current_person_incident_count = None
    if asset and asset.assigned_to_id:
        current_person_incident_count = Incident.query.filter_by(person_id=asset.assigned_to_id).count()

    open_repair = Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first()
    closed_repairs = Repair.query.filter(Repair.asset_tag == asset_tag, Repair.returned_at.isnot(None)) \
        .order_by(Repair.returned_at.desc()).all()

    repair_categories = RepairCategory.query.filter_by(is_active=True).order_by(RepairCategory.name).all()
    custom_field_labels = {f.field_key: f.label for f in CustomField.query.filter_by(entity_type='device').all()}
    pending_action = PendingDeviceAction.query.filter_by(asset_tag=asset_tag, status='pending').first()

    return render_template('admin_assign.html', registry_row=registry_row, asset=asset, has_people=has_people,
                           history=history, combined_history=combined_history, events=events, incidents=incidents,
                           incident_attachments=_attachments_for('incident', [i.id for i in incidents]),
                           signin_flag=next(iter(_signin_mismatches(_current_site_ids(), window_days=90, only_tags=[asset_tag])), None)
                                       if asset and asset.google_last_activity else None,
                           email_enabled=EMAIL_ENABLED,
                           current_person_incident_count=current_person_incident_count,
                           repair_categories=repair_categories,
                           open_repair=open_repair, closed_repairs=closed_repairs, repair_outcomes=REPAIR_OUTCOMES,
                           asset_statuses=ASSET_STATUSES, custom_field_labels=custom_field_labels,
                           now=datetime.utcnow().date(), google_sync_enabled=GOOGLE_SYNC_ENABLED,
                           pending_action=pending_action, action_labels=AUTOMATION_ACTIONS)


@app.route('/admin/bulk_assign', methods=['GET', 'POST'])
@require_permission('devices')
def admin_bulk_assign():
    """
    Bulk-assigns a whole roster in one upload — the start-of-year "hand out
    every Chromebook" workflow. CSV columns: asset_tag, email, due_date (optional).
    People must already exist (use /admin/people or import them first); this
    intentionally does not auto-create people from a typo'd email.
    """
    results = None
    site_ids = _current_site_ids()

    if request.method == 'POST':
        if 'csv_file' not in request.files or not request.files['csv_file'].filename:
            flash('Choose a CSV file to upload.', 'error')
            return redirect(url_for('admin_bulk_assign'))

        file = request.files['csv_file']
        if not file.filename.lower().endswith('.csv'):
            flash('File must be a .csv', 'error')
            return redirect(url_for('admin_bulk_assign'))

        results = []
        try:
            content = file.stream.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
            fieldnames = [(f or '').strip().lower() for f in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames

            if 'asset_tag' not in fieldnames or 'email' not in fieldnames:
                flash(f'CSV must have "asset_tag" and "email" columns. Found: {", ".join(fieldnames)}', 'error')
                return redirect(url_for('admin_bulk_assign'))

            for row in reader:
                asset_tag = (row.get('asset_tag') or '').strip()
                email = (row.get('email') or '').strip().lower()
                due_date_str = (row.get('due_date') or '').strip()

                if not asset_tag or not email:
                    results.append({'asset_tag': asset_tag or '(blank)', 'email': email, 'ok': False,
                                    'message': 'Missing asset_tag or email.'})
                    continue

                registry_row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
                if not registry_row:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'Asset tag not found in registry.'})
                    continue
                if site_ids is not None and registry_row.site_id not in site_ids:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'That device belongs to a different site.'})
                    continue

                person = Person.query.filter(db.func.lower(Person.email) == email).first()
                if not person:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': 'No person with this email — add them first.'})
                    continue
                if not person.is_active:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': f'{person.full_name} is graduated/inactive — reactivate first.'})
                    continue
                if site_ids is not None and person.site_id not in site_ids:
                    results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                    'message': f'{person.full_name} belongs to a different site.'})
                    continue

                due_date = None
                if due_date_str:
                    try:
                        due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        results.append({'asset_tag': asset_tag, 'email': email, 'ok': False,
                                        'message': f'Invalid due_date "{due_date_str}" (use YYYY-MM-DD).'})
                        continue

                status, message = _assign_asset_to_person(asset_tag, person, due_date=due_date)
                if status == 'assigned':
                    emit('device.assigned', registry_row, person=person)
                if status == 'assigned' and registry_row.site_id and person.site_id and registry_row.site_id != person.site_id:
                    message += ' (different sites)'
                results.append({'asset_tag': asset_tag, 'email': email, 'ok': status != 'error', 'message': message})

        except Exception as e:
            flash(f'Bulk assign failed: {e}', 'error')
            return redirect(url_for('admin_bulk_assign'))

        succeeded = sum(1 for r in results if r['ok'])
        flash(f'Assigned {succeeded} of {len(results)} row(s). See details below.',
              'success' if succeeded == len(results) else 'info')

    return render_template('admin_bulk_assign.html', results=results)


@app.route('/admin/bulk_print')
@require_permission('devices')
def admin_bulk_print():
    """
    Lists devices to print labels for (defaults to currently-assigned ones —
    the "just handed out a cart of Chromebooks" case) with checkboxes; actual
    printing happens client-side via the DYMO SDK, looping over the selection
    in the same order the rows appear in the table.

    order=scan lists devices in the order they were scanned during an Asset
    Audit session instead of alphabetical by tag — so labels print in the same
    sequence they were physically handled, and can be applied stack-by-stack
    without hunting back through everything already set aside.
    """
    type_filter = request.args.get('device_type', '').strip()
    order_mode = request.args.get('order', 'tag').strip()
    if order_mode not in ('tag', 'scan'):
        order_mode = 'tag'
    default_status = '' if order_mode == 'scan' else 'assigned'
    status_filter = request.args.get('status', default_status).strip()

    since_str = request.args.get('since', '').strip()
    try:
        since = datetime.strptime(since_str, '%Y-%m-%d') if since_str else None
    except ValueError:
        since = None
    if not since:
        since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    since_str = since.strftime('%Y-%m-%d')

    if type_filter not in DEVICE_TYPES:
        type_filter = ''
    if status_filter not in ASSET_STATUSES:
        status_filter = ''

    site_ids = _current_site_ids()

    if order_mode == 'scan':
        # First-scan time per tag today (or since the given date) gives the
        # exact physical walk order; re-scanning a tag doesn't move it later.
        scan_times = (db.session.query(AuditScan.asset_tag, db.func.min(AuditScan.scanned_at))
                      .filter(AuditScan.scanned_at >= since)
                      .group_by(AuditScan.asset_tag)
                      .order_by(db.func.min(AuditScan.scanned_at))
                      .all())
        ordered_tags = [tag for tag, _ in scan_times]
        registry_by_tag = {r.asset_tag: r for r in _scope_registry(AssetRegistry.query, site_ids).filter(
            AssetRegistry.asset_tag.in_(ordered_tags)
        )}
        rows = [registry_by_tag[t] for t in ordered_tags if t in registry_by_tag]
        if type_filter:
            rows = [r for r in rows if r.device_type == type_filter]
    else:
        query = _scope_registry(AssetRegistry.query, site_ids).order_by(AssetRegistry.asset_tag)
        if type_filter:
            query = query.filter(AssetRegistry.device_type == type_filter)
        rows = query.all()

    assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(
        Asset.asset_tag.in_([r.asset_tag for r in rows])
    )}

    if status_filter:
        def _matches_status(row):
            asset = assets_by_tag.get(row.asset_tag)
            current = asset.status if asset else 'available'
            return current == status_filter
        rows = [r for r in rows if _matches_status(r)]

    candidates = []
    for row in rows:
        asset = assets_by_tag.get(row.asset_tag)
        person = asset.assigned_to if asset else None
        candidates.append({'asset_tag': row.asset_tag, 'person_name': person.full_name if person else '',
                           'is_loaner': row.is_loaner, 'loaner_label': row.loaner_label or ''})

    return render_template('admin_bulk_print.html', candidates=candidates,
                           status_filter=status_filter, type_filter=type_filter,
                           order_mode=order_mode, since=since_str,
                           asset_statuses=ASSET_STATUSES, device_types=DEVICE_TYPES,
                           avery_templates=AVERY_TEMPLATES)


@app.route('/admin/labels/avery', methods=['POST'])
@require_permission('devices')
def admin_avery_labels():
    """Renders the selected asset tags (from Bulk Print's checkboxes, in
    table order) onto an Avery sheet layout. skip = how many labels on the
    first sheet are already used, so a half-used sheet isn't wasted."""
    template_key = request.form.get('template', '5160')
    template = AVERY_TEMPLATES.get(template_key)
    if not template:
        abort(400)
    per_sheet = template['cols'] * template['rows']
    skip = max(0, min(request.form.get('skip', 0, type=int) or 0, per_sheet - 1))
    include_chargers = request.form.get('chargers') == 'on'
    # One newline-joined field rather than one field per tag — a whole
    # site's worth of tags would blow past Werkzeug's 1000-form-part limit.
    tags = [t.strip() for t in request.form.get('asset_tags', '').split('\n') if t.strip()]

    registry = {r.asset_tag: r for r in _scope_registry(AssetRegistry.query, _current_site_ids())
                .filter(AssetRegistry.asset_tag.in_(tags))}
    assets = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(list(registry)))}

    labels = []
    for tag in tags:
        row = registry.get(tag)
        if not row:
            continue  # out of scope or deleted since the page loaded
        asset = assets.get(tag)
        if row.is_loaner:
            second = 'LOANER' + (f': {row.loaner_label}' if row.loaner_label else '')
        else:
            second = asset.assigned_to.full_name if asset and asset.assigned_to else ''
        try:
            barcode = code128_svg(tag)
        except ValueError:
            barcode = None
        labels.append({'tag': tag, 'second': second, 'barcode': barcode, 'charger': False})
        if include_chargers:
            labels.append({'tag': tag, 'second': 'CHARGER' + (f' · {second}' if second else ''),
                           'barcode': barcode, 'charger': True})

    if not labels:
        flash('Select at least one device to print.', 'error')
        return redirect(url_for('admin_bulk_print'))

    slots = [None] * skip + labels
    sheets = [slots[i:i + per_sheet] for i in range(0, len(slots), per_sheet)]
    _log_activity('labels_print', f'Printed {len(labels)} Avery {template_key} label(s).')
    db.session.commit()
    return render_template('admin_avery_labels.html', template=template, template_key=template_key,
                           sheets=sheets, label_count=len(labels), skip=skip)


@app.route('/admin/assets/<string:asset_tag>/unassign', methods=['POST'])
@require_permission('devices')
def admin_asset_unassign(asset_tag):
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    asset = Asset.query.filter_by(asset_tag=asset_tag).first_or_404()
    condition_in = request.form.get('condition_in', '').strip() or None
    try:
        previous_holder = asset.assigned_to
        _close_open_assignment(asset_tag, condition_in=condition_in)
        asset.assigned_to_id = None
        asset.status = 'available'
        _log_activity('device_unassign', f'Unassigned {asset_tag}.', site_id=registry_row.site_id)
        db.session.commit()
        emit('device.unassigned', registry_row, person=previous_holder)
        flash(f'Unassigned {asset_tag}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not unassign asset: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/assets/<string:asset_tag>/status', methods=['POST'])
@require_permission('devices')
def admin_asset_status(asset_tag):
    """Manual status override, independent of assignment. 'repair' can't be set
    directly here — use Send to Repair below, which also creates the tracking
    record. If a status change here bypasses an open Repair some other way,
    that Repair is auto-closed rather than left dangling (same precedent as
    admin_registry_delete auto-closing an open LoanerCheckout)."""
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    new_status = request.form.get('status', '')
    if new_status not in ASSET_STATUSES:
        flash('Invalid status.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))
    if new_status == 'repair':
        flash('Use "Send to Repair" below to mark a device as in repair — it keeps a tracking record.', 'error')
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))

    asset = Asset.query.filter_by(asset_tag=asset_tag).first()
    try:
        if not asset:
            asset = Asset(asset_tag=asset_tag, is_valid=True)
            db.session.add(asset)
        asset.status = new_status
        open_repair = Repair.query.filter_by(asset_tag=asset_tag, returned_at=None).first()
        if open_repair:
            open_repair.returned_at = datetime.utcnow()
            open_repair.notes = ((open_repair.notes + ' ') if open_repair.notes else '') + '[auto-closed: status changed manually]'
        _log_activity('device_status', f'Set {asset_tag} status to {new_status}.', site_id=registry_row.site_id)
        db.session.commit()
        emit('device.status_changed', registry_row)
        flash(f'{asset_tag} status set to {new_status}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not update status: {e}', 'error')
    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))
