"""Pages: licenses, subscriptions and contracts, with when each ends."""
import csv
import io
from decimal import Decimal

from flask import Response, flash, redirect, render_template, request, url_for

from foxdesk.clock import utcnow
from foxdesk.core import app, db
from foxdesk.models import CONTRACT_KINDS, Contract, Site
from foxdesk.services.auth import _has_permission, _log_activity, require_permission
from foxdesk.services.contracts import kind_label, points_for, status_of
from foxdesk.services.util import _parse_date, _parse_money

BILLING = {'yearly': 'Yearly', 'monthly': 'Monthly', 'one-time': 'One time'}
STATUS_ORDER = {'ended': 0, 'renewing': 1, 'active': 2, 'open': 3}


def _yearly(c):
    if c.cost is None:
        return Decimal('0')
    return Decimal(c.cost) * 12 if c.billing == 'monthly' else (Decimal(c.cost) if c.billing != 'one-time' else Decimal('0'))


@app.route('/admin/contracts')
@require_permission('devices')
def admin_contracts():
    today = utcnow().date()
    show = request.args.get('show', 'active')
    query = Contract.query
    if show != 'all':
        query = query.filter(Contract.is_active.is_(True))
    rows = [(c, *status_of(c, today)) for c in query.all()]
    rows.sort(key=lambda r: (STATUS_ORDER[r[1]], r[0].ends_on or today.replace(year=today.year + 50), r[0].name.lower()))
    if request.args.get('export'):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['Name', 'Vendor', 'Kind', 'School', 'Seats', 'Cost', 'Billing', 'Starts', 'Ends', 'Auto-renews', 'Status',
                    'Reference', 'Owner'])
        for c, _, label in rows:
            w.writerow([c.name, c.vendor or '', kind_label(c), c.site.name if c.site else 'District', c.seats or '',
                        f'{c.cost:.2f}' if c.cost is not None else '', BILLING.get(c.billing, ''), c.starts_on or '',
                        c.ends_on or '', 'yes' if c.auto_renews else '', label, c.reference or '', c.owner or ''])
        return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="contracts.csv"'})
    return render_template('admin_contracts.html', rows=rows, show=show, kinds=CONTRACT_KINDS, billing=BILLING,
                           yearly=sum((_yearly(c) for c, key, _ in rows if c.is_active and key != 'ended'), Decimal('0')),
                           can_edit=_has_permission('admin'))


def _apply(c, form):
    name = form.get('name', '').strip()[:160]
    if not name:
        return 'Give it a name.'
    c.name = name
    c.vendor = form.get('vendor', '').strip()[:160] or None
    c.kind = form.get('kind') if form.get('kind') in CONTRACT_KINDS else 'other'
    c.site_id = form.get('site_id', type=int) or None
    c.seats = form.get('seats', type=int)
    c.cost = _parse_money(form.get('cost'))
    c.billing = form.get('billing') if form.get('billing') in BILLING else None
    c.reference = form.get('reference', '').strip()[:120] or None
    c.starts_on = _parse_date(form.get('starts_on'))
    c.ends_on = _parse_date(form.get('ends_on'))
    if c.starts_on and c.ends_on and c.ends_on < c.starts_on:
        return 'It ends before it starts.'
    c.auto_renews = bool(form.get('auto_renews'))
    notice = form.get('notice_days', type=int)
    c.notice_days = min(max(notice if notice is not None else 60, 0), 365)
    c.owner = form.get('owner', '').strip()[:160] or None
    c.notes = form.get('notes', '').strip()[:4000] or None
    return None


@app.route('/admin/contracts/new', methods=['GET', 'POST'])
@app.route('/admin/contracts/<int:contract_id>/edit', methods=['GET', 'POST'])
@require_permission('admin')
def admin_contract_form(contract_id=None):
    contract = db.get_or_404(Contract, contract_id) if contract_id else None
    if request.method == 'POST':
        if contract and request.form.get('action') == 'toggle':
            contract.is_active = not contract.is_active
            _log_activity('settings_edit', f'{"Restored" if contract.is_active else "Archived"} contract "{contract.name}".')
            db.session.commit()
            flash(f'{contract.name} {"is back on the list" if contract.is_active else "is archived (no more reminders)"}.', 'success')
            return redirect(url_for('admin_contracts'))
        target = contract or Contract(is_active=True)
        problem = _apply(target, request.form)
        if problem:
            flash(problem, 'error')
            return render_template('admin_contract_form.html', contract=contract, form=request.form, kinds=CONTRACT_KINDS,
                                   billing=BILLING, sites=Site.query.order_by(Site.name).all())
        if contract is None:
            db.session.add(target)
        _log_activity('settings_edit', f'{"Updated" if contract else "Added"} contract "{target.name}"'
                      + (f', ends {target.ends_on}' if target.ends_on else '') + '.')
        db.session.commit()
        flash(f'Saved {target.name}.' + (f' Reminders at {", ".join(str(p) for p in points_for(target))} days before it ends.'
                                         if target.ends_on else ''), 'success')
        return redirect(url_for('admin_contracts'))
    return render_template('admin_contract_form.html', contract=contract, form=None, kinds=CONTRACT_KINDS, billing=BILLING,
                           sites=Site.query.order_by(Site.name).all())
