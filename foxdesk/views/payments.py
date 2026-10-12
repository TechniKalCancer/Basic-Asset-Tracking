"""Pages: Payments — the dashboard, and every charge in one list to work on
in bulk (record payments, waive, reopen, export)."""
import csv
import io

from flask import Response, flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.models import PAYMENT_METHODS
from foxdesk.services.auth import _current_actor, _current_site_ids, _has_permission, require_permission
from foxdesk.services.billing import AGE_BUCKETS, KINDS, aging_chart, by_keys, charges, mark_paid, reopen, summary, waive
from foxdesk.services.features import feature_enabled
from foxdesk.services.scoping import _sites_for_actor

STATUSES = [('owed', 'Owed'), ('paid', 'Paid'), ('waived', 'Waived'), ('all', 'Everything')]


def _all_rows():
    return charges(_current_site_ids(), include_tickets=_has_permission('tickets'),
                   include_plans=feature_enabled('agreements') and _has_permission('people'))


def _filter(rows, args):
    status = args.get('status', 'owed')
    if status in ('owed', 'paid', 'waived'):
        rows = [c for c in rows if c.status == status]
    kind = args.get('kind')
    if kind in KINDS:
        rows = [c for c in rows if c.kind == kind]
    site_id = args.get('site_id', type=int)
    if site_id:
        rows = [c for c in rows if c.site_id == site_id]
    age = args.get('age', type=int)
    if age:
        rows = [c for c in rows if c.age_days > age]
    q = (args.get('q') or '').strip().lower()
    if q:
        rows = [c for c in rows if q in c.person_name.lower() or q in (c.description or '').lower()
                or q in (c.link_label or '').lower()]
    sort = args.get('sort', 'newest')
    if sort == 'oldest':
        rows = sorted(rows, key=lambda c: c.created_at)
    elif sort == 'person':
        rows = sorted(rows, key=lambda c: (c.person_name.lower(), c.created_at))
    elif sort == 'amount':
        rows = sorted(rows, key=lambda c: -c.amount)
    return rows


@app.route('/admin/payments')
@require_permission('devices')
def admin_payments():
    rows = _all_rows()
    sites = _sites_for_actor(_current_site_ids())
    s = summary(rows, sites)
    return render_template('admin_payments.html', s=s, chart=aging_chart(s['aging']), kinds=KINDS,
                           online=feature_enabled('online_payments'))


@app.route('/admin/payments/charges')
@require_permission('devices')
def admin_payment_charges():
    rows = _filter(_all_rows(), request.args)
    return render_template('admin_payment_charges.html', rows=rows, total=sum((c.amount for c in rows), 0), args=request.args,
                           statuses=STATUSES, kinds=KINDS, methods=PAYMENT_METHODS, ages=AGE_BUCKETS[:-1],
                           sites=_sites_for_actor(_current_site_ids()))


@app.route('/admin/payments/charges/bulk', methods=['POST'])
@require_permission('devices')
def admin_payment_charges_bulk():
    keys = request.form.getlist('charge')
    back = url_for('admin_payment_charges', **{k: v for k, v in request.args.items()})
    if not keys:
        flash('Tick at least one charge.', 'error')
        return redirect(back)
    rows = by_keys(_all_rows(), keys)
    _, actor_label, _ = _current_actor()
    action = request.form.get('action')
    if action == 'paid':
        done = mark_paid(rows, request.form.get('method'), request.form.get('note'), actor_label)
        message = f'Recorded {done} payment(s).'
    elif action == 'waive':
        reason = (request.form.get('reason') or '').strip()
        if not reason:
            flash('Say why these are being waived — it stays with each charge.', 'error')
            return redirect(back)
        done = waive(rows, reason, actor_label)
        message = f'Waived {done} charge(s).'
    elif action == 'reopen':
        done = reopen(rows, actor_label)
        message = f'Reopened {done} waived charge(s); they\'re owed again.'
    else:
        flash('Choose what to do with them.', 'error')
        return redirect(back)
    db.session.commit()
    skipped = len(rows) - done
    flash(message + (f' {skipped} skipped (already settled or not waived).' if skipped else ''), 'success' if done else 'info')
    return redirect(back)


@app.route('/admin/payments/charges/export')
@require_permission('devices')
def admin_payment_charges_export():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Date (UTC)', 'Person', 'Type', 'For', 'Reference', 'Amount', 'Status', 'Paid', 'Method', 'Waived', 'Waived by',
                'Reason'])
    for c in _filter(_all_rows(), request.args):
        w.writerow([f'{c.created_at:%Y-%m-%d}', c.person_name, c.kind_label, c.description, c.link_label or '',
                    f'{c.amount:.2f}', c.status, f'{c.paid_at:%Y-%m-%d}' if c.paid_at else '', c.method_label,
                    f'{c.waived_at:%Y-%m-%d}' if c.waived_at else '', c.waived_by or '', c.waive_reason or ''])
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="charges.csv"'})
