"""Pages: receipts (list, print, settings) and printable invoices for home."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import APP_URL, app, db
from foxdesk.models import BrandingSettings, Person, Receipt
from foxdesk.services.auth import _current_actor, _current_site_ids, _log_activity, require_permission
from foxdesk.services.features import feature_enabled
from foxdesk.services.receipts import invoice_for, invoice_settings, method_label, receipts_for_earlier
from foxdesk.services.scoping import _scope_people, _sites_for_actor

MAX_PRINT = 400


def _scope_receipts(query):
    site_ids = _current_site_ids()
    return query if site_ids is None else query.filter(Receipt.site_id.in_(site_ids or [-1]))


def _paper_context():
    """What every printed page needs: branding, the settings, and where to pay online."""
    online = None
    if APP_URL and feature_enabled('online_payments') and (feature_enabled('portal') or feature_enabled('parent_portal')):
        online = f'{APP_URL}/my'
    return dict(branding_settings=db.session.get(BrandingSettings, 1), settings=invoice_settings(), pay_online_url=online)


def _ids(name):
    return [int(x) for x in request.args.getlist(name) if str(x).isdigit()][:MAX_PRINT]


@app.route('/admin/receipts')
@require_permission('devices')
def admin_receipts():
    q = request.args.get('q', '').strip()
    query = _scope_receipts(Receipt.query)
    if q:
        number = q.upper().removeprefix('R-').lstrip('0')
        query = query.filter(db.or_(Receipt.person_name.ilike(f'%{q}%'),
                                    Receipt.id == int(number) if number.isdigit() else db.false()))
    site_id = request.args.get('site_id', type=int)
    if site_id:
        query = query.filter(Receipt.site_id == site_id)
    receipts = query.order_by(Receipt.id.desc()).limit(300).all()
    return render_template('admin_receipts.html', receipts=receipts, q=q, site_id=site_id,
                           sites=_sites_for_actor(_current_site_ids()), settings=invoice_settings())


@app.route('/admin/receipts/settings', methods=['POST'])
@require_permission('admin')
def admin_receipt_settings():
    row = invoice_settings(create=True)
    row.payable_to = request.form.get('payable_to', '').strip()[:160] or None
    for field in ('pay_instructions', 'contact', 'footer'):
        setattr(row, field, request.form.get(field, '').strip()[:2000] or None)
    _log_activity('settings_edit', 'Updated what invoices and receipts say about paying.')
    db.session.commit()
    flash('Saved. Invoices and receipts print with it from now on.', 'success')
    return redirect(url_for('admin_receipts'))


@app.route('/admin/receipts/<int:receipt_id>')
@require_permission('devices')
def admin_receipt(receipt_id):
    receipt = _scope_receipts(Receipt.query).filter(Receipt.id == receipt_id).first_or_404()
    return render_template('admin_receipt_print.html', receipts=[receipt], **_paper_context())


@app.route('/admin/receipts/print')
@require_permission('devices')
def admin_receipts_print():
    ids = _ids('id')
    receipts = _scope_receipts(Receipt.query).filter(Receipt.id.in_(ids)).order_by(Receipt.id).all() if ids else []
    if not receipts:
        flash('Those receipts weren\'t found. Pick one from the list.', 'info')
        return redirect(url_for('admin_receipts'))
    return render_template('admin_receipt_print.html', receipts=receipts, **_paper_context())


@app.route('/admin/people/<int:person_id>/invoice')
@require_permission('devices')
def admin_person_invoice(person_id):
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    return render_template('admin_invoice_print.html', invoices=[invoice_for(person)], **_paper_context())


@app.route('/admin/invoices/print')
@require_permission('devices')
def admin_invoices_print():
    """Invoices for several students, one per page: ?person=1&person=2, or
    ?owing=1 (every student who owes anything, optionally ?site_id=)."""
    people_query = _scope_people(Person.query, _current_site_ids())
    if request.args.get('owing'):
        from foxdesk.services.billing import charges
        site_id = request.args.get('site_id', type=int)
        owed_ids = {c.person_id for c in charges(_current_site_ids()) if c.status == 'owed' and c.person_id
                    and (not site_id or c.site_id == site_id)}
        people = people_query.filter(Person.id.in_(owed_ids)).order_by(Person.last_name, Person.first_name).limit(MAX_PRINT).all()
    else:
        ids = _ids('person')
        people = people_query.filter(Person.id.in_(ids)).order_by(Person.last_name, Person.first_name).all() if ids else []
    invoices = [inv for inv in (invoice_for(p) for p in people) if inv['owed']]
    if not invoices:
        flash('Nobody selected owes anything, so there\'s no invoice to print.', 'info')
        return redirect(request.referrer or url_for('admin_payment_charges'))
    return render_template('admin_invoice_print.html', invoices=invoices, **_paper_context())


@app.route('/admin/people/<int:person_id>/receipts_for_earlier', methods=['POST'])
@require_permission('devices')
def admin_person_receipts_for_earlier(person_id):
    person = _scope_people(Person.query, _current_site_ids()).filter_by(id=person_id).first_or_404()
    _, actor_label, _ = _current_actor()
    made = receipts_for_earlier(person.id, actor_label)
    db.session.commit()
    if made:
        return redirect(url_for('admin_receipts_print', id=[r.id for r in made]))
    flash('Every payment already has a receipt.', 'info')
    return redirect(request.referrer or url_for('admin_person', person_id=person.id))


def _receipt_balance(receipt):
    """What the receipt's person still owes today (printed on the receipt)."""
    if not receipt.person_id:
        return 0
    from foxdesk.services.billing import person_charges
    return sum(c.amount for c in person_charges(receipt.person_id) if c.status == 'owed')


app.jinja_env.globals.update(receipt_method=method_label, receipt_balance=_receipt_balance)
