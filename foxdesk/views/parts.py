"""Pages: parts inventory (Repairs → Parts)."""
from decimal import Decimal, InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.models import PART_CATEGORIES, AssetRegistry, DeviceModel, Part, PartMovement, Repair, Site, Ticket
from foxdesk.services.auth import _current_site_ids, _log_activity, require_permission
from foxdesk.services.parts import low_stock_query, model_names, record_movement, use_part
from foxdesk.services.scoping import _scope_registry, _scope_tickets


def _scoped_parts():
    """Parts kept at one of the user's sites, or not tied to a site."""
    site_ids = _current_site_ids()
    query = Part.query
    if site_ids is not None:
        query = query.filter(db.or_(Part.site_id.is_(None), Part.site_id.in_(site_ids)))
    return query


def _parts_for(asset_tag=None):
    """[(part, fits_this_model)] for the use-a-part picker: parts for the device's model first."""
    row = AssetRegistry.query.filter_by(asset_tag=asset_tag).first() if asset_tag else None
    model_id = row.device_model_id if row else None
    parts = _scoped_parts().filter(Part.is_active.is_(True)).order_by(Part.category, Part.name).all()
    fits = [(p, True) for p in parts if model_id and model_id in (p.fits_models or [])]
    return fits + [(p, False) for p in parts if not p.fits_models]


def _parts_used_on(repair=None, ticket=None):
    query = PartMovement.query.filter(PartMovement.reason == 'used')
    if repair is not None:
        query = query.filter(PartMovement.repair_id == repair.id)
    elif ticket is not None:
        query = query.filter(PartMovement.ticket_id == ticket.id)
    else:
        return []
    return query.order_by(PartMovement.id).all()


app.jinja_env.globals.update(parts_for=_parts_for, parts_used_on=_parts_used_on)


@app.route('/admin/parts')
@require_permission('repairs')
def admin_parts():
    show = request.args.get('show', '')
    category = request.args.get('category', '')
    query = _scoped_parts().filter(Part.is_active.is_(show != 'inactive'))
    if show == 'low':
        query = query.filter(Part.reorder_level > 0, Part.quantity_on_hand <= Part.reorder_level)
    if category in PART_CATEGORIES:
        query = query.filter(Part.category == category)
    parts = query.order_by(Part.category, Part.name).all()
    models = {m.id: m.full_name for m in DeviceModel.query.all()}
    return render_template('admin_parts.html', parts=parts, show=show, category=category, categories=PART_CATEGORIES,
                           models=models, low_count=low_stock_query().count(),
                           stock_value=sum((p.unit_cost or 0) * p.quantity_on_hand for p in parts))


def _part_from_form(part):
    part.name = request.form.get('name', '').strip()[:160]
    part.part_number = request.form.get('part_number', '').strip()[:120] or None
    part.category = request.form.get('category') if request.form.get('category') in PART_CATEGORIES else 'other'
    known = {m.id for m in DeviceModel.query.all()}
    part.fits_models = sorted({int(i) for i in request.form.getlist('fits_models') if i.isdigit() and int(i) in known}) or None
    site_id = request.form.get('site_id', type=int)
    site_ids = _current_site_ids()
    part.site_id = site_id if site_id and (site_ids is None or site_id in site_ids) else None
    part.reorder_level = max(0, request.form.get('reorder_level', type=int) or 0)
    cost = request.form.get('unit_cost', '').strip().lstrip('$')
    try:
        part.unit_cost = Decimal(cost).quantize(Decimal('0.01')) if cost else None
    except InvalidOperation:
        raise ValueError('The unit cost isn\'t a number.')
    part.vendor = request.form.get('vendor', '').strip()[:160] or None
    part.notes = request.form.get('notes', '').strip() or None
    if not part.name:
        raise ValueError('Give the part a name.')


def _render_form(part):
    site_ids = _current_site_ids()
    sites = Site.query.order_by(Site.name).all() if site_ids is None else Site.query.filter(Site.id.in_(site_ids)).all()
    return render_template('admin_part_form.html', part=part, categories=PART_CATEGORIES, sites=sites,
                           device_models=DeviceModel.query.order_by(DeviceModel.manufacturer, DeviceModel.model_name).all())


@app.route('/admin/parts/new', methods=['GET', 'POST'])
@require_permission('repairs')
def admin_part_new():
    part = Part(category='other', reorder_level=0, quantity_on_hand=0, is_active=True)
    if request.method == 'POST':
        try:
            _part_from_form(part)
            db.session.add(part)
            db.session.flush()
            starting = request.form.get('quantity', type=int) or 0
            if starting:
                record_movement(part, starting, 'received', note='Starting count')
            _log_activity('part_add', f'Added part {part.name}.', site_id=part.site_id)
            db.session.commit()
            flash(f'Added {part.name}.', 'success')
            return redirect(url_for('admin_part_detail', part_id=part.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
    return _render_form(part)


@app.route('/admin/parts/<int:part_id>/edit', methods=['GET', 'POST'])
@require_permission('repairs')
def admin_part_edit(part_id):
    part = _scoped_parts().filter_by(id=part_id).first_or_404()
    if request.method == 'POST':
        try:
            _part_from_form(part)
            part.is_active = request.form.get('is_active') == 'on'
            _log_activity('part_edit', f'Edited part {part.name}.', site_id=part.site_id)
            db.session.commit()
            flash(f'Saved {part.name}.', 'success')
            return redirect(url_for('admin_part_detail', part_id=part.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
    return _render_form(part)


@app.route('/admin/parts/<int:part_id>')
@require_permission('repairs')
def admin_part_detail(part_id):
    part = _scoped_parts().filter_by(id=part_id).first_or_404()
    return render_template('admin_part_detail.html', part=part, movements=part.movements.limit(200).all(),
                           fits=model_names(part.fits_models))


@app.route('/admin/parts/<int:part_id>/stock', methods=['POST'])
@require_permission('repairs')
def admin_part_stock(part_id):
    """Received, returned, or a count correction (set the count to what's on the shelf)."""
    part = _scoped_parts().filter_by(id=part_id).first_or_404()
    action = request.form.get('action')
    try:
        if action == 'count':
            counted = request.form.get('counted', type=int)
            if counted is None or counted < 0:
                raise ValueError('Enter how many are on the shelf.')
            record_movement(part, counted - part.quantity_on_hand, 'adjusted', note=request.form.get('note'))
        elif action in ('received', 'returned'):
            qty = request.form.get('quantity', type=int) or 0
            if qty < 1:
                raise ValueError('Enter how many.')
            record_movement(part, qty, action, note=request.form.get('note'))
        else:
            abort(400)
        db.session.commit()
        flash(f'{part.name}: {part.quantity_on_hand} on hand.', 'success')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(url_for('admin_part_detail', part_id=part.id))


@app.route('/admin/parts/use', methods=['POST'])
@require_permission('repairs')
def admin_part_use():
    """Use a part on a repair or ticket — the form on those pages posts here."""
    site_ids = _current_site_ids()
    part = _scoped_parts().filter_by(id=request.form.get('part_id', type=int), is_active=True).first()
    repair = ticket = None
    if request.form.get('repair_id', type=int):
        repair = Repair.query.get_or_404(request.form.get('repair_id', type=int))
        _scope_registry(AssetRegistry.query, site_ids).filter_by(asset_tag=repair.asset_tag).first_or_404()
        back = url_for('admin_repair_detail', repair_id=repair.id)
    elif request.form.get('ticket_id', type=int):
        ticket = _scope_tickets(Ticket.query, site_ids).filter_by(id=request.form.get('ticket_id', type=int)).first_or_404()
        back = url_for('admin_ticket_detail', ticket_id=ticket.id)
    else:
        abort(400)
    if not part:
        flash('Pick a part.', 'error')
        return redirect(back + '#parts')
    try:
        use_part(part, request.form.get('quantity', type=int) or 1, repair=repair, ticket=ticket,
                 charge=request.form.get('charge') == 'on')
        db.session.commit()
        flash(f'Used {part.name}. {part.quantity_on_hand} left.' + (' Running low.' if part.is_low else ''),
              'success')
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'error')
    return redirect(back + '#parts')
