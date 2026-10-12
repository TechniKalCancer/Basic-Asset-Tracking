"""Pages: equipment (servers, network gear, printers, displays), the E-rate
inventory, and locations."""
import csv
import io

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from foxdesk.core import app, db
from foxdesk.models import (
    EQUIPMENT_TYPES, FUNDING_SOURCES, LOCATION_KINDS, Asset, AssetRegistry, DeviceModel, DeviceRecord, Location, Site,
)
from foxdesk.services.auth import _current_site_ids, _log_activity, require_permission
from foxdesk.services.equipment import (
    descendant_ids, equipment_query, locations_for, support_days_left, type_label,
)
from foxdesk.services.scoping import _scope_registry, _sites_for_actor
from foxdesk.services.util import _parse_date, _parse_money


def _equipment_or_404(asset_tag):
    row = _scope_registry(AssetRegistry.query, _current_site_ids()).filter_by(asset_tag=asset_tag).first_or_404()
    return row


def _filtered(args):
    site_ids = _current_site_ids()
    query = equipment_query(_scope_registry(AssetRegistry.query, site_ids))
    if args.get('type') in EQUIPMENT_TYPES:
        query = query.filter(AssetRegistry.device_type == args['type'])
    site_id = args.get('site_id', type=int)
    if site_id:
        query = query.filter(AssetRegistry.site_id == site_id)
    location_id = args.get('location_id', type=int)
    if location_id:
        loc = db.session.get(Location, location_id)
        query = query.filter(AssetRegistry.location_id.in_(descendant_ids(loc) if loc else [-1]))
    q = (args.get('q') or '').strip()
    if q:
        like = f'%{q}%'
        query = query.outerjoin(DeviceModel, DeviceModel.id == AssetRegistry.device_model_id).filter(db.or_(
            AssetRegistry.asset_tag.ilike(like), AssetRegistry.hostname.ilike(like), AssetRegistry.ip_address.ilike(like),
            AssetRegistry.serial_number.ilike(like), AssetRegistry.description.ilike(like),
            DeviceModel.model_name.ilike(like), DeviceModel.manufacturer.ilike(like)))
    rows = query.order_by(AssetRegistry.site_id, AssetRegistry.device_type, AssetRegistry.asset_tag).all()
    support = args.get('support')
    if support == 'ending':
        rows = [r for r in rows if (support_days_left(r) is not None and 0 <= support_days_left(r) <= 365)]
    elif support == 'past':
        rows = [r for r in rows if (support_days_left(r) is not None and support_days_left(r) < 0)]
    elif support == 'down':
        down = {r.registry_id for r in DeviceRecord.query.filter_by(source='librenms')
                if r.registry_id and not (r.raw or {}).get('up', True)}
        rows = [r for r in rows if r.id in down]
    return rows


@app.route('/admin/equipment')
@require_permission('devices')
def admin_equipment():
    rows = _filtered(request.args)
    counts = dict(equipment_query(_scope_registry(AssetRegistry.query, _current_site_ids()))
                  .with_entities(AssetRegistry.device_type, db.func.count(AssetRegistry.id))
                  .group_by(AssetRegistry.device_type).all())
    site_ids = _current_site_ids()
    return render_template('admin_equipment.html', rows=rows, counts=counts, types=EQUIPMENT_TYPES,
                           sites=_sites_for_actor(site_ids), locations=locations_for(site_ids), args=request.args)


@app.route('/admin/equipment/export')
@require_permission('devices')
def admin_equipment_export():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Asset tag', 'Type', 'Make', 'Model', 'Serial', 'Hostname', 'IP', 'MAC', 'Firmware', 'School', 'Location',
                'Rack', 'Purchased', 'Cost', 'Warranty ends', 'Support ends', 'End of support', 'Funding', 'FRN',
                'Funding year'])
    for r in _filtered(request.args):
        w.writerow([r.asset_tag, type_label(r.device_type), r.device_model.manufacturer if r.device_model else '',
                    r.device_model.model_name if r.device_model else (r.description or ''), r.serial_number or '',
                    r.hostname or '', r.ip_address or '', r.mac_address or '', r.firmware or '',
                    r.site.name if r.site else '', r.location.path if r.location else '', r.rack_position or '',
                    r.purchase_date or '', r.purchase_cost if r.purchase_cost is not None else '',
                    r.warranty_expiration or '', r.support_ends or '', r.end_of_support or '', r.funding_source or '',
                    r.erate_frn or '', r.funding_year or ''])
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="equipment.csv"'})


# ─── add / edit ───────────────────────────────────────────────────────────────

FIELDS = ('serial_number', 'description', 'hostname', 'ip_address', 'mac_address', 'firmware', 'management_url',
          'rack_position', 'funding_source', 'erate_frn')


def _form_values(form, site_ids):
    values = {f: (form.get(f) or '').strip() or None for f in FIELDS}
    values['device_type'] = form.get('device_type') if form.get('device_type') in EQUIPMENT_TYPES else 'switch'
    values['device_model_id'] = form.get('device_model_id', type=int) or None
    values['site_id'] = form.get('site_id', type=int) or None
    values['location_id'] = form.get('location_id', type=int) or None
    values['funding_year'] = form.get('funding_year', type=int) or None
    for f in ('purchase_date', 'warranty_expiration', 'support_ends', 'end_of_support'):
        values[f] = _parse_date(form.get(f))
    values['purchase_cost'] = _parse_money(form.get('purchase_cost'))
    if values['management_url'] and not values['management_url'].startswith(('http://', 'https://')):
        values['management_url'] = 'https://' + values['management_url']
    problems = []
    if site_ids is not None and values['site_id'] not in site_ids:
        problems.append('Choose one of your own schools.')
    loc = db.session.get(Location, values['location_id']) if values['location_id'] else None
    if values['location_id'] and (loc is None or loc.site_id != values['site_id']):
        problems.append('That location is at a different school.')
    if values['device_model_id'] and not db.session.get(DeviceModel, values['device_model_id']):
        values['device_model_id'] = None
    return values, problems


def _form_page(row, values):
    site_ids = _current_site_ids()
    return render_template('admin_equipment_form.html', row=row, values=values, types=EQUIPMENT_TYPES,
                           sites=_sites_for_actor(site_ids), locations=locations_for(site_ids),
                           models=DeviceModel.query.order_by(DeviceModel.manufacturer, DeviceModel.model_name).all(),
                           funding_sources=FUNDING_SOURCES)


@app.route('/admin/equipment/new', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_equipment_new():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        values, problems = _form_values(request.form, site_ids)
        tag = (request.form.get('asset_tag') or '').strip() or values['hostname']
        if not tag:
            problems.append('Give it an asset tag (or a hostname to use as one).')
        if problems:
            for p in problems:
                flash(p, 'error')
            return _form_page(None, dict(values, asset_tag=tag))
        row = AssetRegistry(asset_tag=tag[:120], **values)
        try:
            db.session.add(row)
            db.session.flush()
            if not Asset.query.filter_by(asset_tag=row.asset_tag).first():
                db.session.add(Asset(asset_tag=row.asset_tag, is_valid=True, status='available'))
            _log_activity('device_add', f'Added {type_label(row.device_type).lower()} {row.asset_tag}.', site_id=row.site_id)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('That asset tag or serial number is already in use.', 'error')
            return _form_page(None, dict(values, asset_tag=tag))
        flash(f'Added {row.asset_tag}.', 'success')
        return redirect(url_for('admin_asset_assign', asset_tag=row.asset_tag))
    values = {'device_type': request.args.get('type') if request.args.get('type') in EQUIPMENT_TYPES else 'switch',
              'location_id': request.args.get('location_id', type=int)}
    if values['location_id']:
        loc = db.session.get(Location, values['location_id'])
        values['site_id'] = loc.site_id if loc else None
    return _form_page(None, values)


@app.route('/admin/equipment/<path:asset_tag>/edit', methods=['GET', 'POST'])
@require_permission('devices_manage')
def admin_equipment_edit(asset_tag):
    row = _equipment_or_404(asset_tag)
    site_ids = _current_site_ids()
    if request.method == 'POST':
        values, problems = _form_values(request.form, site_ids)
        if problems:
            for p in problems:
                flash(p, 'error')
            return _form_page(row, values)
        for key, value in values.items():
            setattr(row, key, value)
        try:
            _log_activity('device_edit', f'Edited {type_label(row.device_type).lower()} {row.asset_tag}.', site_id=row.site_id)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('That serial number is already on another device.', 'error')
            return _form_page(row, values)
        flash('Saved.', 'success')
        return redirect(url_for('admin_asset_assign', asset_tag=row.asset_tag))
    values = {c: getattr(row, c) for c in FIELDS + ('device_type', 'device_model_id', 'site_id', 'location_id', 'funding_year',
                                                    'purchase_date', 'warranty_expiration', 'support_ends',
                                                    'end_of_support', 'purchase_cost')}
    values['asset_tag'] = row.asset_tag
    return _form_page(row, values)


# ─── E-rate inventory ─────────────────────────────────────────────────────────

def _erate_rows():
    query = _scope_registry(AssetRegistry.query, _current_site_ids()).filter(db.or_(
        AssetRegistry.funding_source.ilike('%e-rate%'), AssetRegistry.funding_source.ilike('%erate%'),
        AssetRegistry.erate_frn.isnot(None)))
    year = request.args.get('year', type=int)
    if year:
        query = query.filter(AssetRegistry.funding_year == year)
    return query.order_by(AssetRegistry.funding_year.desc().nullslast(), AssetRegistry.erate_frn,
                          AssetRegistry.asset_tag).all()


@app.route('/admin/equipment/erate')
@require_permission('devices')
def admin_erate_inventory():
    rows = _erate_rows()
    years = sorted({y for (y,) in db.session.query(AssetRegistry.funding_year).filter(AssetRegistry.funding_year.isnot(None))
                    .distinct()}, reverse=True)
    status = {a.asset_tag: a.status for a in Asset.query.filter(Asset.asset_tag.in_([r.asset_tag for r in rows]))} if rows else {}
    return render_template('admin_erate.html', rows=rows, years=years, year=request.args.get('year', type=int), status=status)


@app.route('/admin/equipment/erate/export')
@require_permission('devices')
def admin_erate_export():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['FRN', 'Funding year', 'Funding', 'Make', 'Model', 'Serial number', 'Asset tag', 'Type', 'School',
                'Building / room', 'Installed (purchase date)', 'Cost', 'Status'])
    status = {a.asset_tag: a.status for a in Asset.query}
    for r in _erate_rows():
        w.writerow([r.erate_frn or '', r.funding_year or '', r.funding_source or '',
                    r.device_model.manufacturer if r.device_model else '',
                    r.device_model.model_name if r.device_model else (r.description or ''), r.serial_number or '',
                    r.asset_tag, type_label(r.device_type), r.site.name if r.site else '',
                    r.location.path if r.location else '', r.purchase_date or '',
                    r.purchase_cost if r.purchase_cost is not None else '', status.get(r.asset_tag, '')])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="erate-inventory.csv"'})


# ─── locations ────────────────────────────────────────────────────────────────

def _location_or_404(location_id):
    loc = db.get_or_404(Location, location_id)
    site_ids = _current_site_ids()
    if site_ids is not None and loc.site_id not in site_ids:
        abort(404)
    return loc


@app.route('/admin/locations', methods=['GET', 'POST'])
@require_permission('devices')
def admin_locations():
    site_ids = _current_site_ids()
    if request.method == 'POST':
        from foxdesk.services.auth import _has_permission
        if not _has_permission('devices_manage'):
            abort(403)
        name = (request.form.get('name') or '').strip()[:120]
        site_id = request.form.get('site_id', type=int)
        parent = db.session.get(Location, request.form.get('parent_id', type=int)) if request.form.get('parent_id') else None
        kind = request.form.get('kind') if request.form.get('kind') in LOCATION_KINDS else 'room'
        if parent is not None:
            site_id = parent.site_id
        if not name or not site_id or not db.session.get(Site, site_id) or (site_ids is not None and site_id not in site_ids):
            flash('Give the location a name and a school.', 'error')
            return redirect(url_for('admin_locations'))
        same_parent = Location.parent_id == parent.id if parent else Location.parent_id.is_(None)
        if Location.query.filter(Location.site_id == site_id, db.func.lower(Location.name) == name.lower(), same_parent).first():
            flash(f'"{name}" is already there.', 'error')
            return redirect(url_for('admin_locations'))
        loc = Location(site_id=site_id, name=name, kind=kind, parent_id=parent.id if parent else None,
                       notes=(request.form.get('notes') or '').strip() or None)
        db.session.add(loc)
        _log_activity('location_add', f'Added location {name}.', site_id=site_id)
        db.session.commit()
        flash(f'Added {loc.full_path}.', 'success')
        return redirect(url_for('admin_locations'))
    tree = locations_for(site_ids)
    counts = dict(db.session.query(AssetRegistry.location_id, db.func.count(AssetRegistry.id))
                  .filter(AssetRegistry.location_id.isnot(None)).group_by(AssetRegistry.location_id).all())
    return render_template('admin_locations.html', tree=tree, counts=counts, kinds=LOCATION_KINDS,
                           sites=_sites_for_actor(site_ids))


@app.route('/admin/locations/<int:location_id>', methods=['POST'])
@require_permission('devices_manage')
def admin_location_edit(location_id):
    loc = _location_or_404(location_id)
    if request.form.get('action') == 'delete':
        if loc.children or AssetRegistry.query.filter_by(location_id=loc.id).first():
            flash(f'{loc.name} still has devices or other locations in it. Move them first.', 'error')
        else:
            _log_activity('location_delete', f'Deleted location {loc.full_path}.', site_id=loc.site_id)
            db.session.delete(loc)
            db.session.commit()
            flash('Deleted.', 'success')
        return redirect(url_for('admin_locations'))
    name = (request.form.get('name') or '').strip()[:120]
    if name:
        loc.name = name
    if request.form.get('kind') in LOCATION_KINDS:
        loc.kind = request.form['kind']
    loc.notes = (request.form.get('notes') or '').strip() or None
    _log_activity('location_edit', f'Edited location {loc.full_path}.', site_id=loc.site_id)
    db.session.commit()
    flash('Saved.', 'success')
    return redirect(url_for('admin_locations'))

