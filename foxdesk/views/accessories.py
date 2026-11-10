"""Pages: accessory types (what goes out with a device, and what it costs if lost)."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.models import AccessoryType, DEVICE_TYPE_LABELS, END_USER_TYPES
from foxdesk.services.auth import _log_activity, require_permission
from foxdesk.services.util import _parse_money


def _device_types():
    return {k: DEVICE_TYPE_LABELS.get(k, (k.title(),))[0] for k in END_USER_TYPES}


def _apply(t, form):
    name = form.get('name', '').strip()[:80]
    if not name:
        return 'Give the accessory a name.'
    t.name = name
    device_type = form.get('device_type', '').strip()
    t.device_type = device_type if device_type in END_USER_TYPES else None
    t.fee = _parse_money(form.get('fee'))
    t.default_on = bool(form.get('default_on'))
    t.sort_order = form.get('sort_order', type=int) or 0
    return None


@app.route('/admin/accessories', methods=['GET', 'POST'])
@require_permission('devices')
def admin_accessories():
    if request.method == 'POST':
        t = AccessoryType(is_active=True)
        problem = _apply(t, request.form)
        if problem:
            flash(problem, 'error')
        else:
            db.session.add(t)
            _log_activity('device_edit', f'Added accessory "{t.name}".')
            db.session.commit()
            flash(f'Added {t.name}.', 'success')
        return redirect(url_for('admin_accessories'))
    types = AccessoryType.query.order_by(AccessoryType.is_active.desc(), AccessoryType.sort_order, AccessoryType.name).all()
    return render_template('admin_accessories.html', types=types, device_types=_device_types())


@app.route('/admin/accessories/<int:type_id>', methods=['POST'])
@require_permission('devices')
def admin_accessory_edit(type_id):
    t = db.get_or_404(AccessoryType, type_id)
    if request.form.get('action') == 'toggle':
        t.is_active = not t.is_active
        message = f'{t.name} is {"back in use" if t.is_active else "retired (kept on past assignments)"}.'
    else:
        problem = _apply(t, request.form)
        if problem:
            flash(problem, 'error')
            return redirect(url_for('admin_accessories'))
        message = f'Saved {t.name}.'
    _log_activity('device_edit', f'Accessory "{t.name}": {message}')
    db.session.commit()
    flash(message, 'success')
    return redirect(url_for('admin_accessories'))
