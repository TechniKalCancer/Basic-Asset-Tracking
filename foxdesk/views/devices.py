"""Pages: devices."""
import csv
import io
from datetime import datetime
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from foxdesk.core import app, db
from foxdesk.models import (
    ASSET_STATUSES,
    Asset,
    AssetNumberRange,
    AssetRegistry,
    DEVICE_TYPES,
    DeviceModel,
    LoanerCheckout,
    Person,
    Site,
)
from foxdesk.services.util import (
    _generate_asset_tag,
    _next_tag_in_range,
    _parse_date,
    _parse_money,
    heal_orphans,
    resolve_scan,
)
from foxdesk.services.auth import (
    _current_site_ids,
    _log_activity,
    login_required,
    require_permission,
    require_super_admin,
)
from foxdesk.services.scoping import (
    _active_device_models,
    _filter_registry_by_status,
    _filter_registry_by_warranty,
    _person_search_filter,
    _scope_people,
    _scope_registry,
    _sites_for_actor,
)
from foxdesk.integrations.google import _push_asset_tag_to_google
from foxdesk.services.assignments import _close_open_assignment


@app.route('/admin/upload_csv', methods=['POST'])
@require_super_admin
def upload_csv():
    """
    Accepts a CSV with columns: asset_tag, serial_number (optional), description (optional).
    Completely replaces the registry (every site's devices, not just one) — so
    this is super-admin only. Site-scoped admins use /admin/registry/set_sites
    or the People-import-style upsert instead. Heals any orphaned Asset records afterwards.
    """
    if 'csv_file' not in request.files:
        flash('No file part in request', 'error')
        return redirect(url_for('admin_panel'))

    file = request.files['csv_file']
    if not file.filename.lower().endswith('.csv'):
        flash('File must be a .csv', 'error')
        return redirect(url_for('admin_panel'))

    try:
        content = file.stream.read().decode('utf-8-sig')  # strips BOM

        # Auto-detect delimiter from first line
        first_line = content.splitlines()[0] if content.splitlines() else ''
        delimiter = '\t' if '\t' in first_line else ','

        # Read raw headers from first line and normalize them
        raw_headers = next(csv.reader([first_line], delimiter=delimiter))
        normalized_headers = [h.strip().lower().replace(' ', '_') for h in raw_headers]

        # Feed remaining content to DictReader with normalized headers
        stream = io.StringIO(content)
        reader = csv.DictReader(stream, delimiter=delimiter)
        reader.fieldnames = normalized_headers
        next(reader)  # skip the original header row

        # Accept MDM column names "asset_id" / "asset id" as well as "asset_tag"
        TAG_COLS    = ('asset_id', 'asset_tag')
        SERIAL_COLS = ('serial_number',)
        DESC_COLS   = ('description',)
        TYPE_COLS   = ('device_type', 'type')
        PURCHASE_DATE_COLS = ('purchase_date',)
        PURCHASE_COST_COLS = ('purchase_cost', 'cost')
        WARRANTY_COLS      = ('warranty_expiration', 'warranty')

        tag_col    = next((c for c in TAG_COLS    if c in normalized_headers), None)
        serial_col = next((c for c in SERIAL_COLS if c in normalized_headers), None)
        desc_col   = next((c for c in DESC_COLS   if c in normalized_headers), None)
        type_col   = next((c for c in TYPE_COLS   if c in normalized_headers), None)
        purchase_date_col = next((c for c in PURCHASE_DATE_COLS if c in normalized_headers), None)
        purchase_cost_col = next((c for c in PURCHASE_COST_COLS if c in normalized_headers), None)
        warranty_col       = next((c for c in WARRANTY_COLS if c in normalized_headers), None)

        if not any((tag_col, serial_col, desc_col, type_col)):
            flash(f'CSV must have at least one recognized column (asset_tag, serial_number, '
                  f'description, or device_type). Found: {", ".join(normalized_headers)}', 'error')
            return redirect(url_for('admin_panel'))

        rows = list(reader)

        # Wipe old registry and replace
        AssetRegistry.query.delete()
        db.session.flush()

        imported = 0
        skipped  = 0
        auto_assigned = 0
        seen_tags    = set()
        seen_serials = set()

        def clean(val):
            """Return None if value is empty, '0', or whitespace."""
            v = (val or '').strip()
            return None if (not v or v == '0') else v

        for row in rows:
            tag    = clean(row.get(tag_col, ''))    if tag_col    else None
            serial = clean(row.get(serial_col, '')) if serial_col else None
            desc   = clean(row.get(desc_col, ''))   if desc_col   else None
            dtype  = clean(row.get(type_col, ''))   if type_col   else None
            dtype  = dtype.lower() if dtype and dtype.lower() in DEVICE_TYPES else 'chromebook'
            purchase_date = _parse_date(row.get(purchase_date_col, '')) if purchase_date_col else None
            purchase_cost = _parse_money(row.get(purchase_cost_col, '')) if purchase_cost_col else None
            warranty_expiration = _parse_date(row.get(warranty_col, '')) if warranty_col else None

            # If asset_id is missing but serial exists, use serial as the tag
            if not tag and serial:
                tag = serial

            # Skip rows where every recognized column is blank (e.g. stray blank CSV lines)
            if not tag and not serial and not desc:
                skipped += 1
                continue

            # Still no tag (no asset_id/serial given) but the row has real data — self-assign one
            if not tag:
                tag = _generate_asset_tag(seen_tags)
                auto_assigned += 1

            # Skip duplicate tags
            if tag in seen_tags:
                skipped += 1
                continue

            # Drop duplicate serial but keep the tag
            if serial and serial in seen_serials:
                serial = None

            seen_tags.add(tag)
            if serial:
                seen_serials.add(serial)

            db.session.add(AssetRegistry(
                asset_tag=tag,
                serial_number=serial,
                description=desc,
                device_type=dtype,
                purchase_date=purchase_date,
                purchase_cost=purchase_cost,
                warranty_expiration=warranty_expiration,
            ))
            imported += 1

        _log_activity('registry_csv_import', f'Replaced the asset registry via CSV upload: {imported} row(s) imported, {skipped} skipped.')
        db.session.commit()
        healed = heal_orphans()

        msg = f'Imported {imported} assets.'
        if auto_assigned:
            msg += f' Self-assigned a tag for {auto_assigned} row{"s" if auto_assigned != 1 else ""} with none given.'
        if skipped:
            msg += f' Skipped {skipped} duplicate/invalid rows.'
        if healed:
            msg += f' Healed {healed} previously-unknown asset record(s).'
        flash(msg, 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Import failed: {e}', 'error')

    return redirect(url_for('admin_panel'))


@app.route('/admin/registry/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_new():
    """Manually adds a single device. Leave asset_tag blank to self-assign one —
    either a random 6-digit tag (default), or the next unused number in a
    specific reserved range if one's picked from the "Generate From Range"
    dropdown (matches a physical batch of pre-printed labels: grab the next
    sticker in the stack, in order, rather than a random one)."""
    site_ids = _current_site_ids()
    sites = _sites_for_actor(site_ids)
    device_models = _active_device_models()
    asset_number_ranges = AssetNumberRange.query.order_by(AssetNumberRange.range_start).all()
    if request.method == 'POST':
        tag = request.form.get('asset_tag', '').strip()
        range_id = request.form.get('range_id', type=int)
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        device_model_id = request.form.get('device_model_id', type=int)
        site_id = request.form.get('site_id', type=int)
        purchase_date = _parse_date(request.form.get('purchase_date'))
        purchase_cost = _parse_money(request.form.get('purchase_cost'))
        warranty_expiration = _parse_date(request.form.get('warranty_expiration'))

        if not serial:
            flash('Serial number is required.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if tag and AssetRegistry.query.filter_by(asset_tag=tag).first():
            flash(f'Asset tag "{tag}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if AssetRegistry.query.filter_by(serial_number=serial).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

        if not tag:
            existing_tags = {t for (t,) in db.session.query(AssetRegistry.asset_tag).all()}
            if range_id:
                asset_range = AssetNumberRange.query.get(range_id)
                if not asset_range:
                    flash('That reserved range no longer exists.', 'error')
                    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)
                tag = _next_tag_in_range(existing_tags, asset_range.range_start, asset_range.range_end)
                if not tag:
                    flash(f'"{asset_range.label}" is fully used — every number from {asset_range.range_start} to {asset_range.range_end} is already in the registry.', 'error')
                    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)
            else:
                tag = _generate_asset_tag(existing_tags)

        try:
            new_row = AssetRegistry(
                asset_tag=tag, serial_number=serial,
                description=description, device_type=device_type, device_model_id=device_model_id, site_id=site_id,
                purchase_date=purchase_date, purchase_cost=purchase_cost,
                warranty_expiration=warranty_expiration,
            )
            db.session.add(new_row)
            _log_activity('device_add', f'Added device {tag} to the registry.', site_id=site_id)
            db.session.commit()
            # Heals a matching orphan scan record immediately, same effect as
            # upload_csv's heal_orphans() but without waiting for the next
            # bulk import — matters for the "Add to Registry" flow from the
            # Orphaned Records page.
            orphan = Asset.query.filter_by(asset_tag=tag, is_valid=False).first()
            if orphan:
                orphan.is_valid = True
                db.session.commit()
            _push_asset_tag_to_google(new_row)
            flash(f'Added device {tag} to the registry.', 'success')
            return redirect(url_for('admin_asset_assign', asset_tag=tag))
        except IntegrityError as e:
            db.session.rollback()
            flash('Could not add device: that asset tag or serial number is already in use.', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)
        except Exception as e:
            db.session.rollback()
            flash(f'Could not add device: {e}', 'error')
            return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=request.form, sites=sites)

    prefill_tag = request.args.get('asset_tag', '').strip()
    prefill_form = {'asset_tag': prefill_tag} if prefill_tag else None
    return render_template('admin_registry_new.html', device_types=DEVICE_TYPES, device_models=device_models, asset_number_ranges=asset_number_ranges, form=prefill_form, sites=sites)


@app.route('/admin/registry/quick_add', methods=['POST'])
@require_permission('devices_manage')
def admin_registry_quick_add():
    """Bulk-intake flow for unboxing a shipment: scan a serial, hit enter,
    repeat — creates a minimal AssetRegistry row (asset tag auto-generated)
    and redirects right back to the registry list, never to the full Add
    Device form, so a USB barcode scanner (which types the serial + Enter)
    can just keep firing without the admin touching anything between scans.
    Device type and site are carried back via querystring so the dropdowns
    stay put for the next scan instead of resetting to their defaults."""
    site_ids = _current_site_ids()
    serial = request.form.get('serial_number', '').strip()
    device_type = request.form.get('device_type', 'chromebook').strip()
    device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
    site_id = request.form.get('site_id', type=int)
    sticky = {'quick_device_type': device_type, 'quick_site_id': site_id}

    if not serial:
        flash('Scan or type a serial number.', 'error')
        return redirect(url_for('admin_registry', **sticky))
    if site_ids is not None and (not site_id or site_id not in site_ids):
        flash('Choose one of your own sites.', 'error')
        return redirect(url_for('admin_registry'))
    if AssetRegistry.query.filter_by(serial_number=serial).first():
        flash(f'A device with serial number "{serial}" already exists.', 'error')
        return redirect(url_for('admin_registry', **sticky))

    existing_tags = {t for (t,) in db.session.query(AssetRegistry.asset_tag).all()}
    tag = _generate_asset_tag(existing_tags)
    try:
        db.session.add(AssetRegistry(asset_tag=tag, serial_number=serial, device_type=device_type, site_id=site_id))
        _log_activity('device_add', f'Quick-added device {tag} (serial {serial}) to the registry.', site_id=site_id)
        db.session.commit()
        orphan = Asset.query.filter_by(asset_tag=tag, is_valid=False).first()
        if orphan:
            orphan.is_valid = True
            db.session.commit()
        flash(f'Added {tag} (serial {serial}) — ready for the next scan.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Could not add device: that asset tag or serial number is already in use.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not add device: {e}', 'error')
    return redirect(url_for('admin_registry', **sticky))


@app.route('/admin/registry/<string:asset_tag>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_edit(asset_tag):
    """Edits a device's attributes (serial, description, type, site). The
    asset_tag itself isn't editable here — it's the key used everywhere
    else (assignments, history, events), so renaming it is out of scope."""
    site_ids = _current_site_ids()
    registry_row = _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=asset_tag).first_or_404()
    sites = _sites_for_actor(site_ids)
    device_models = _active_device_models()

    if request.method == 'POST':
        serial = request.form.get('serial_number', '').strip() or None
        description = request.form.get('description', '').strip() or None
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        device_model_id = request.form.get('device_model_id', type=int)
        site_id = request.form.get('site_id', type=int)
        purchase_date = _parse_date(request.form.get('purchase_date'))
        purchase_cost = _parse_money(request.form.get('purchase_cost'))
        warranty_expiration = _parse_date(request.form.get('warranty_expiration'))

        if not serial:
            flash('Serial number is required.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)

        if site_ids is not None and (not site_id or site_id not in site_ids):
            flash('Choose one of your own sites.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)

        if AssetRegistry.query.filter(AssetRegistry.serial_number == serial,
                                       AssetRegistry.asset_tag != asset_tag).first():
            flash(f'A device with serial number "{serial}" already exists.', 'error')
            return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)

        try:
            registry_row.serial_number = serial
            registry_row.description = description
            registry_row.device_type = device_type
            registry_row.device_model_id = device_model_id
            registry_row.site_id = site_id
            registry_row.purchase_date = purchase_date
            registry_row.purchase_cost = purchase_cost
            registry_row.warranty_expiration = warranty_expiration
            _log_activity('device_edit', f'Edited device {asset_tag}.', site_id=site_id)
            db.session.commit()
            _push_asset_tag_to_google(registry_row)  # cheap no-op if it's already correct in Google; matters when a serial is added/corrected here
            flash(f'Updated {asset_tag}.', 'success')
            return redirect(url_for('admin_registry'))
        except Exception as e:
            db.session.rollback()
            flash(f'Could not update device: {e}', 'error')

    return render_template('admin_registry_edit.html', registry_row=registry_row, device_types=DEVICE_TYPES, device_models=device_models, sites=sites)


DEVICE_MODEL_SORT_COLUMNS = {
    'manufacturer': (DeviceModel.manufacturer, DeviceModel.model_name),
    'model_name': (DeviceModel.model_name,),
    'device_type': (DeviceModel.device_type,),
}


@app.route('/admin/device_models')
@require_permission('devices_manage')
def admin_device_models():
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'manufacturer').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in DEVICE_MODEL_SORT_COLUMNS:
        sort = 'manufacturer'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = DeviceModel.query
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(DeviceModel.manufacturer.ilike(like), DeviceModel.model_name.ilike(like),
                                     DeviceModel.notes.ilike(like)))
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()).nullslast() for c in DEVICE_MODEL_SORT_COLUMNS[sort]]
    models = query.order_by(*order_exprs).all()
    return render_template('admin_device_models.html', models=models, search=search, sort=sort, sort_dir=sort_dir)


@app.route('/admin/device_models/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_device_model_new():
    if request.method == 'POST':
        manufacturer = request.form.get('manufacturer', '').strip()
        model_name = request.form.get('model_name', '').strip()
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        notes = request.form.get('notes', '').strip() or None

        if not manufacturer or not model_name:
            flash('Manufacturer and model name are required.', 'error')
            return render_template('admin_device_model_form.html', model=None, device_types=DEVICE_TYPES, form=request.form)
        if DeviceModel.query.filter(db.func.lower(DeviceModel.manufacturer) == manufacturer.lower(),
                                     db.func.lower(DeviceModel.model_name) == model_name.lower()).first():
            flash(f'"{manufacturer} {model_name}" already exists.', 'error')
            return render_template('admin_device_model_form.html', model=None, device_types=DEVICE_TYPES, form=request.form)

        model = DeviceModel(manufacturer=manufacturer, model_name=model_name, device_type=device_type, notes=notes)
        db.session.add(model)
        _log_activity('device_model_add', f'Added device model "{manufacturer} {model_name}".')
        db.session.commit()
        flash(f'Added "{manufacturer} {model_name}".', 'success')
        return redirect(url_for('admin_device_models'))

    return render_template('admin_device_model_form.html', model=None, device_types=DEVICE_TYPES, form=None)


@app.route('/admin/device_models/<int:model_id>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_device_model_edit(model_id):
    model = DeviceModel.query.get_or_404(model_id)
    if request.method == 'POST':
        manufacturer = request.form.get('manufacturer', '').strip()
        model_name = request.form.get('model_name', '').strip()
        device_type = request.form.get('device_type', 'chromebook').strip()
        device_type = device_type if device_type in DEVICE_TYPES else 'chromebook'
        notes = request.form.get('notes', '').strip() or None
        is_active = bool(request.form.get('is_active'))

        if not manufacturer or not model_name:
            flash('Manufacturer and model name are required.', 'error')
            return render_template('admin_device_model_form.html', model=model, device_types=DEVICE_TYPES, form=None)
        if DeviceModel.query.filter(db.func.lower(DeviceModel.manufacturer) == manufacturer.lower(),
                                     db.func.lower(DeviceModel.model_name) == model_name.lower(),
                                     DeviceModel.id != model_id).first():
            flash(f'"{manufacturer} {model_name}" already exists.', 'error')
            return render_template('admin_device_model_form.html', model=model, device_types=DEVICE_TYPES, form=None)

        model.manufacturer = manufacturer
        model.model_name = model_name
        model.device_type = device_type
        model.notes = notes
        model.is_active = is_active
        _log_activity('device_model_edit', f'Edited device model "{manufacturer} {model_name}".')
        db.session.commit()
        flash(f'Updated "{manufacturer} {model_name}".', 'success')
        return redirect(url_for('admin_device_models'))

    return render_template('admin_device_model_form.html', model=model, device_types=DEVICE_TYPES, form=None)


@app.route('/admin/device_models/<int:model_id>/delete', methods=['POST'])
@require_permission('devices_manage')
def admin_device_model_delete(model_id):
    model = DeviceModel.query.get_or_404(model_id)
    in_use = AssetRegistry.query.filter_by(device_model_id=model_id).count()
    if in_use:
        flash(f'Cannot delete "{model.full_name}" — {in_use} device(s) still reference it.', 'error')
        return redirect(url_for('admin_device_models'))
    label = model.full_name
    db.session.delete(model)
    _log_activity('device_model_delete', f'Deleted device model "{label}".')
    db.session.commit()
    flash(f'Deleted "{label}".', 'success')
    return redirect(url_for('admin_device_models'))


ASSET_NUMBER_RANGE_SORT_COLUMNS = {
    'label': (AssetNumberRange.label,),
    'range_start': (AssetNumberRange.range_start,),
}


@app.route('/admin/asset_number_ranges')
@require_permission('devices_manage')
def admin_asset_number_ranges():
    search = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'range_start').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in ASSET_NUMBER_RANGE_SORT_COLUMNS:
        sort = 'range_start'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = AssetNumberRange.query
    if search:
        query = query.filter(AssetNumberRange.label.ilike(f'%{search}%'))
    order_exprs = [(c.desc() if sort_dir == 'desc' else c.asc()) for c in ASSET_NUMBER_RANGE_SORT_COLUMNS[sort]]
    ranges = query.order_by(*order_exprs).all()
    return render_template('admin_asset_number_ranges.html', ranges=ranges, search=search, sort=sort, sort_dir=sort_dir)


def _parse_asset_range_bounds(form):
    """Returns (range_start, range_end, error) — error is a flashable string, or None if valid."""
    start = form.get('range_start', type=int)
    end = form.get('range_end', type=int)
    if start is None or end is None or start < 0 or end < 0:
        return None, None, 'Start and end must be positive whole numbers.'
    if start > end:
        return None, None, 'Start must be less than or equal to end.'
    return start, end, None


@app.route('/admin/asset_number_ranges/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_asset_number_range_new():
    if request.method == 'POST':
        label = request.form.get('label', '').strip()
        notes = request.form.get('notes', '').strip() or None
        start, end, error = _parse_asset_range_bounds(request.form)

        if not label:
            flash('Label is required.', 'error')
            return render_template('admin_asset_number_range_form.html', range=None, form=request.form)
        if error:
            flash(error, 'error')
            return render_template('admin_asset_number_range_form.html', range=None, form=request.form)

        db.session.add(AssetNumberRange(label=label, range_start=start, range_end=end, notes=notes))
        _log_activity('asset_number_range_add', f'Reserved asset tag range "{label}" ({start}-{end}).')
        db.session.commit()
        flash(f'Reserved range "{label}" ({start}-{end}).', 'success')
        return redirect(url_for('admin_asset_number_ranges'))

    return render_template('admin_asset_number_range_form.html', range=None, form=None)


@app.route('/admin/asset_number_ranges/<int:range_id>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_asset_number_range_edit(range_id):
    asset_range = AssetNumberRange.query.get_or_404(range_id)
    if request.method == 'POST':
        label = request.form.get('label', '').strip()
        notes = request.form.get('notes', '').strip() or None
        start, end, error = _parse_asset_range_bounds(request.form)

        if not label:
            flash('Label is required.', 'error')
            return render_template('admin_asset_number_range_form.html', range=asset_range, form=None)
        if error:
            flash(error, 'error')
            return render_template('admin_asset_number_range_form.html', range=asset_range, form=None)

        asset_range.label = label
        asset_range.range_start = start
        asset_range.range_end = end
        asset_range.notes = notes
        _log_activity('asset_number_range_edit', f'Edited reserved range "{label}" ({start}-{end}).')
        db.session.commit()
        flash(f'Updated range "{label}".', 'success')
        return redirect(url_for('admin_asset_number_ranges'))

    return render_template('admin_asset_number_range_form.html', range=asset_range, form=None)


@app.route('/admin/asset_number_ranges/<int:range_id>/set_default', methods=['POST'])
@require_permission('devices_manage')
def admin_asset_number_range_set_default(range_id):
    """Toggles this range as THE default _generate_asset_tag() draws from —
    at most one range is ever default, so setting one clears any other."""
    asset_range = AssetNumberRange.query.get_or_404(range_id)
    if asset_range.is_default:
        asset_range.is_default = False
        _log_activity('asset_number_range_edit', f'Unset "{asset_range.label}" as the default range.')
        flash(f'"{asset_range.label}" is no longer the default range.', 'success')
    else:
        AssetNumberRange.query.filter(AssetNumberRange.id != range_id).update({'is_default': False})
        asset_range.is_default = True
        _log_activity('asset_number_range_edit', f'Set "{asset_range.label}" as the default range.')
        flash(f'"{asset_range.label}" is now the default range — Add Device and CSV import will pull from it automatically.', 'success')
    db.session.commit()
    return redirect(url_for('admin_asset_number_ranges'))


@app.route('/admin/asset_number_ranges/<int:range_id>/delete', methods=['POST'])
@require_permission('devices_manage')
def admin_asset_number_range_delete(range_id):
    asset_range = AssetNumberRange.query.get_or_404(range_id)
    label = asset_range.label
    db.session.delete(asset_range)
    _log_activity('asset_number_range_delete', f'Deleted reserved range "{label}".')
    db.session.commit()
    flash(f'Deleted range "{label}".', 'success')
    return redirect(url_for('admin_asset_number_ranges'))


@app.route('/admin/registry/<string:asset_tag>/delete', methods=['POST'])
@require_permission('devices_manage')
def admin_registry_delete(asset_tag):
    """
    Permanently removes a device from the registry. Any current assignment is
    closed out first (same pattern as deleting a Person), and an open loaner
    checkout is auto-closed rather than left dangling. AssignmentHistory/Event/
    Incident/LoanerCheckout rows are untouched — they reference asset_tag as a
    plain string, not a foreign key, so history stays intact and readable.
    """
    registry_row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    try:
        open_loaner = LoanerCheckout.query.filter_by(asset_tag=asset_tag, checked_in_at=None).first()
        if open_loaner:
            open_loaner.checked_in_at = datetime.utcnow()
            open_loaner.condition_notes = ((open_loaner.condition_notes + ' ') if open_loaner.condition_notes else '') + '[auto-closed: device deleted]'

        asset = Asset.query.filter_by(asset_tag=asset_tag).first()
        if asset:
            _close_open_assignment(asset_tag, condition_in='Device deleted')
            db.session.delete(asset)

        registry_site_id = registry_row.site_id
        db.session.delete(registry_row)
        _log_activity('device_delete', f'Deleted device {asset_tag} from the registry.', site_id=registry_site_id)
        db.session.commit()
        flash(f'Deleted {asset_tag} from the registry.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete device: {e}', 'error')
    return redirect(url_for('admin_registry'))


@app.route('/admin/registry/set_sites', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_registry_set_sites():
    """
    Non-destructive way to fill in devices' sites via CSV (upsert-by-asset_tag,
    same pattern as the People import) — for cleaning up whatever the one-time
    backfill couldn't infer, without needing the super-admin-only full replace.
    Columns: asset_tag, site.
    """
    results = None
    site_ids = _current_site_ids()

    if request.method == 'POST':
        if 'csv_file' not in request.files or not request.files['csv_file'].filename:
            flash('Choose a CSV file to upload.', 'error')
            return redirect(url_for('admin_registry_set_sites'))

        file = request.files['csv_file']
        if not file.filename.lower().endswith('.csv'):
            flash('File must be a .csv', 'error')
            return redirect(url_for('admin_registry_set_sites'))

        results = []
        try:
            content = file.stream.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
            fieldnames = [(f or '').strip().lower().replace(' ', '_') for f in (reader.fieldnames or [])]
            reader.fieldnames = fieldnames

            if 'asset_tag' not in fieldnames or 'site' not in fieldnames:
                flash(f'CSV must have "asset_tag" and "site" columns. Found: {", ".join(fieldnames)}', 'error')
                return redirect(url_for('admin_registry_set_sites'))

            updated = skipped = 0
            for row in reader:
                tag = (row.get('asset_tag') or '').strip()
                site_name = (row.get('site') or '').strip()
                if not tag or not site_name:
                    skipped += 1
                    results.append({'row': tag or '(blank)', 'ok': False, 'message': 'Missing asset_tag or site.'})
                    continue

                registry_row = AssetRegistry.query.filter_by(asset_tag=tag).first()
                if not registry_row:
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': 'No device with that asset_tag.'})
                    continue
                if site_ids is not None and registry_row.site_id not in (site_ids + [None]):
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': 'That device belongs to a different site.'})
                    continue

                site_row = Site.query.filter(db.func.lower(Site.name) == site_name.lower()).first()
                if not site_row:
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': f'Unknown site "{site_name}".'})
                    continue
                if site_ids is not None and site_row.id not in site_ids:
                    skipped += 1
                    results.append({'row': tag, 'ok': False, 'message': f'"{site_name}" isn\'t one of your sites.'})
                    continue

                registry_row.site_id = site_row.id
                updated += 1
                results.append({'row': tag, 'ok': True, 'message': f'Set {tag} to {site_row.name}.'})

            _log_activity('registry_set_sites', f'Set sites via CSV for {updated} device(s), skipped {skipped}.')
            db.session.commit()
            flash(f'Updated {updated}, skipped {skipped} row(s).', 'success' if not skipped else 'info')
        except Exception as e:
            db.session.rollback()
            flash(f'Import failed: {e}', 'error')
            return redirect(url_for('admin_registry_set_sites'))

    return render_template('admin_registry_set_sites.html', results=results)


REGISTRY_SORT_COLUMNS = {
    'asset_tag': AssetRegistry.asset_tag,
    'serial_number': AssetRegistry.serial_number,
    'description': AssetRegistry.description,
    'device_type': AssetRegistry.device_type,
    'site': Site.name,
    'status': Asset.status,
}


@app.route('/admin/registry')
@require_permission('devices')
def admin_registry():
    page          = request.args.get('page', 1, type=int)
    per_page      = 50
    site_ids      = _current_site_ids()
    query         = _scope_registry(AssetRegistry.query, site_ids)
    search        = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    type_filter   = request.args.get('device_type', '').strip()
    person_filter = request.args.get('person_id', '').strip()
    warranty_filter = request.args.get('warranty', '').strip()

    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                AssetRegistry.asset_tag.ilike(like),
                AssetRegistry.serial_number.ilike(like),
                AssetRegistry.description.ilike(like),
            )
        )
    if status_filter in ASSET_STATUSES:
        query = _filter_registry_by_status(query, status_filter)
    else:
        status_filter = ''
    if type_filter in DEVICE_TYPES:
        query = query.filter(AssetRegistry.device_type == type_filter)
    else:
        type_filter = ''
    if warranty_filter in ('expiring', 'expired'):
        query = _filter_registry_by_warranty(query, warranty_filter)
    else:
        warranty_filter = ''

    person_filter_name = None
    if person_filter.isdigit():
        person = _scope_people(Person.query, site_ids).filter_by(id=int(person_filter)).first()
        if person:
            person_filter_name = person.full_name
            owned_tags = db.session.query(Asset.asset_tag).filter(Asset.assigned_to_id == person.id)
            query = query.filter(AssetRegistry.asset_tag.in_(owned_tags))
        else:
            person_filter = ''
    else:
        person_filter = ''

    sort = request.args.get('sort', 'asset_tag').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort not in REGISTRY_SORT_COLUMNS:
        sort = 'asset_tag'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort == 'site':
        query = query.outerjoin(Site, AssetRegistry.site_id == Site.id)
    elif sort == 'status':
        query = query.outerjoin(Asset, Asset.asset_tag == AssetRegistry.asset_tag)
    sort_col = REGISTRY_SORT_COLUMNS[sort]
    order_expr = sort_col.desc() if sort_dir == 'desc' else sort_col.asc()
    # nulls last regardless of direction — an empty serial/description/site
    # shouldn't dominate either end of the sort
    query = query.order_by(order_expr.nullslast(), AssetRegistry.asset_tag)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    page_tags = [row.asset_tag for row in pagination.items]
    assets_by_tag = {
        a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(page_tags))
    }

    quick_device_type = request.args.get('quick_device_type', 'chromebook').strip()
    quick_device_type = quick_device_type if quick_device_type in DEVICE_TYPES else 'chromebook'
    quick_site_id = request.args.get('quick_site_id', type=int)
    return render_template('admin_registry.html', pagination=pagination, search=search,
                           status_filter=status_filter, asset_statuses=ASSET_STATUSES,
                           type_filter=type_filter, device_types=DEVICE_TYPES,
                           sort=sort, sort_dir=sort_dir,
                           person_filter=person_filter, person_filter_name=person_filter_name,
                           warranty_filter=warranty_filter, today=datetime.utcnow().date(),
                           assets_by_tag=assets_by_tag, sites=_sites_for_actor(site_ids),
                           quick_device_type=quick_device_type, quick_site_id=quick_site_id)


@app.route('/admin/registry/export')
@require_permission('devices')
def admin_registry_export():
    """Exports the full asset list (not just the current page) as CSV, including
    live status and assignment — useful as a backup/reporting snapshot."""
    rows = _scope_registry(AssetRegistry.query, _current_site_ids()).order_by(AssetRegistry.asset_tag).all()
    assets_by_tag = {a.asset_tag: a for a in Asset.query.all()}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['asset_tag', 'serial_number', 'description', 'device_type', 'status', 'assigned_to', 'assigned_to_email',
                      'purchase_date', 'purchase_cost', 'warranty_expiration'])
    for row in rows:
        asset = assets_by_tag.get(row.asset_tag)
        status = asset.status if asset else 'available'
        person = asset.assigned_to if asset else None
        writer.writerow([
            row.asset_tag, row.serial_number or '', row.description or '', row.device_type,
            status, person.full_name if person else '', person.email if person else '',
            row.purchase_date.isoformat() if row.purchase_date else '',
            str(row.purchase_cost) if row.purchase_cost is not None else '',
            row.warranty_expiration.isoformat() if row.warranty_expiration else '',
        ])

    response = app.response_class(buffer.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=asset_export.csv'
    return response


@app.route('/admin/scan_lookup')
@require_permission('devices')
def admin_scan_lookup():
    """
    Jumps straight to an asset's assign page from a scanned/typed tag or serial number.
    Works with any USB barcode scanner, since those just type into the focused
    field and send Enter — no special hardware integration needed.
    """
    value = request.args.get('value', '').strip()
    if not value:
        return redirect(url_for('admin_registry'))

    asset_tag, _ = resolve_scan(value)
    site_ids = _current_site_ids()
    if asset_tag and site_ids is not None:
        row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first()
        if not row or row.site_id not in site_ids:
            asset_tag = None
    if not asset_tag:
        flash(f'No asset found matching "{value}".', 'error')
        return redirect(url_for('admin_registry'))

    return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))


@app.route('/admin/search')
@login_required
def admin_search():
    """
    Global lookup from the nav bar search box — checks both People and Assets
    at once. A single unambiguous match jumps straight to that record instead
    of showing a results page.
    """
    q = request.args.get('q', '').strip()
    people, assets = [], []
    site_ids = _current_site_ids()

    if len(q) >= 2:
        people = _scope_people(Person.query, site_ids).filter(_person_search_filter(q)) \
            .order_by(Person.last_name, Person.first_name).limit(25).all()

        like = f'%{q}%'
        registry_rows = _scope_registry(AssetRegistry.query, site_ids).filter(
            db.or_(
                AssetRegistry.asset_tag.ilike(like),
                AssetRegistry.serial_number.ilike(like),
                AssetRegistry.description.ilike(like),
            )
        ).order_by(AssetRegistry.asset_tag).limit(25).all()
        tags = [r.asset_tag for r in registry_rows]
        assets_by_tag = {a.asset_tag: a for a in Asset.query.filter(Asset.asset_tag.in_(tags))}
        assets = [(r, assets_by_tag.get(r.asset_tag)) for r in registry_rows]

        if len(people) == 1 and not assets:
            return redirect(url_for('admin_registry', person_id=people[0].id))
        if len(assets) == 1 and not people:
            return redirect(url_for('admin_asset_assign', asset_tag=assets[0][0].asset_tag))

    return render_template('admin_search.html', q=q, people=people, assets=assets)


@app.route('/admin/orphans')
@require_super_admin
def admin_orphans():
    """Orphan scans have no matching AssetRegistry row, so there's nothing to
    attribute a site to — kept super-admin-only rather than guessing."""
    search = request.args.get('q', '').strip()
    sort_dir = request.args.get('dir', 'asc').strip()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    query = Asset.query.filter_by(is_valid=False)
    if search:
        query = query.filter(Asset.asset_tag.ilike(f'%{search}%'))
    orphans = query.order_by(Asset.asset_tag.desc() if sort_dir == 'desc' else Asset.asset_tag.asc()).all()
    return render_template('admin_orphans.html', orphans=orphans, search=search, sort_dir=sort_dir)


@app.route('/admin/orphans/<string:asset_tag>/delete', methods=['POST'])
@require_super_admin
def admin_orphan_delete(asset_tag):
    """Permanently removes an orphan scan record — for a typo'd/mis-scanned
    tag that will never be a real device. A tag that IS a real device should
    go through 'Add to Registry' instead, which heals it rather than deleting it."""
    orphan = Asset.query.filter_by(asset_tag=asset_tag, is_valid=False).first_or_404()
    try:
        db.session.delete(orphan)
        _log_activity('orphan_delete', f'Deleted orphaned scan record {asset_tag}.')
        db.session.commit()
        flash(f'Deleted orphaned record {asset_tag}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not delete orphan: {e}', 'error')
    return redirect(url_for('admin_orphans'))
