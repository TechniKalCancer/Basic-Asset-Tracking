"""Pages: Settings → Portals & payments."""
from flask import flash, redirect, render_template, request, url_for

from foxdesk.core import APP_URL, app, db
from foxdesk.models import FeePayment
from foxdesk.services.auth import _log_activity, require_super_admin
from foxdesk.services.features import feature_enabled
from foxdesk.services.labels import qr_svg
from foxdesk.services.payments import PaymentError, check_key, is_test_mode, portal_settings, save_stripe_key, stripe_key


@app.route('/admin/portal', methods=['GET', 'POST'])
@require_super_admin
def admin_portal():
    row = portal_settings()
    if request.method == 'POST':
        try:
            save_stripe_key(request.form.get('stripe_secret_key'))
            row.payment_note = request.form.get('payment_note', '').strip()[:255] or None
            _log_activity('portal_settings', 'Saved portal and payment settings'
                          + (' (new Stripe key)' if request.form.get('stripe_secret_key') else '') + '.')
            db.session.commit()
            flash('Saved.', 'success')
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
        return redirect(url_for('admin_portal'))
    db.session.commit()
    portal_url = (APP_URL or request.url_root.rstrip('/')) + '/my'
    return render_template('admin_portal.html', row=row, portal_url=portal_url, qr=qr_svg(portal_url),
                           has_key=bool(stripe_key()), test_mode=is_test_mode(),
                           on=dict(portal=feature_enabled('portal'), parent=feature_enabled('parent_portal'),
                                   payments=feature_enabled('online_payments'), google=feature_enabled('google_signin')),
                           public=bool(APP_URL),
                           payments=FeePayment.query.order_by(FeePayment.created_at.desc()).limit(50).all())


@app.route('/admin/portal/test_payments', methods=['POST'])
@require_super_admin
def admin_portal_test_payments():
    try:
        check_key()
        flash('Stripe accepted the key' + (' (test mode: no real money moves).' if is_test_mode() else '.'), 'success')
    except PaymentError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_portal'))
