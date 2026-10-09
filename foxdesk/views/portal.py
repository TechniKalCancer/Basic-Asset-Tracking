"""Pages: the portal at /my — "My stuff" for students and staff, and the
parent portal — plus QR-code device links (/r/<asset_tag>)."""
import time
from collections import defaultdict, deque
from urllib.parse import quote

from flask import abort, flash, redirect, render_template, request, session, url_for

from foxdesk.core import APP_URL, EMAIL_ENABLED, app, db
from foxdesk.models import AssetRegistry, Incident, Ticket, TicketCategory, TicketComment
from foxdesk.services.features import feature_enabled
from foxdesk.services.payments import PaymentError, confirm, start_checkout, unpaid_fee
from foxdesk.services.portal import (
    LINK_MINUTES, damage_for, devices_for, fees_for, make_link_token, may_pay, may_see_ticket, redeem_link_token,
    sign_in, sign_out, tickets_for, viewer,
)
from foxdesk.services.signin import SigninError, google_config, redirect_uri, start, verify

_link_requests = defaultdict(deque)  # ip -> recent sign-in link requests (per worker; a soft brake)


def _base_url():
    return APP_URL or request.url_root.rstrip('/')


def _portal_on():
    return feature_enabled('portal') or feature_enabled('parent_portal')


def _require_viewer():
    if not _portal_on():
        abort(404)
    v = viewer()
    if not v:
        return None
    return v


def _payments_on():
    return feature_enabled('online_payments')


def _login_page():
    return render_template('portal_login.html', google=feature_enabled('google_signin') and google_config()['configured'],
                           email_links=EMAIL_ENABLED, next_device=session.get('portal_next_device'))


@app.route('/my')
def portal_home():
    v = _require_viewer()
    if not v:
        return _login_page()
    person = v['person']
    children = [dict(person=c, devices=devices_for(c), damage=damage_for(c)) for c in v['children']]
    return render_template('portal_home.html', v=v, person=person,
                           devices=devices_for(person) if person else [],
                           tickets=tickets_for(person, v['email']) if person else [],
                           fees=fees_for(person) if person else [], children=children,
                           payments=_payments_on(), unpaid=unpaid_fee, payment_note=_payment_note())


def _payment_note():
    from foxdesk.models import PortalSettings
    row = PortalSettings.query.get(1)
    return (row.payment_note if row else None) or 'Pay at your school\'s front office.'


@app.route('/my/login', methods=['POST'])
def portal_login():
    if not _portal_on():
        abort(404)
    ip = request.remote_addr or '?'
    recent = _link_requests[ip]
    while recent and time.time() - recent[0] > 900:
        recent.popleft()
    if len(recent) >= 10:
        flash('Too many requests. Wait a few minutes and try again.', 'error')
        return redirect(url_for('portal_home'))
    recent.append(time.time())
    email = request.form.get('email', '').strip()
    if not EMAIL_ENABLED:
        flash('Email sign-in isn\'t available here. Use Google, or ask the school office.', 'error')
        return redirect(url_for('portal_home'))
    token = make_link_token(email)
    if token:
        from foxdesk.services.emailer import _send_email_in_background
        link = f'{_base_url()}/my/login/{token}'
        _send_email_in_background(email.strip().lower(), 'Your sign-in link',
                                  f'Use this link to sign in. It works once and expires in {LINK_MINUTES} minutes:\n\n'
                                  f'{link}\n\nIf you didn\'t ask for this, you can ignore this email.')
    # Same answer either way, so the form can't be used to find out who's on file.
    flash(f'If {email} is on file with the school, a sign-in link is on its way. It expires in {LINK_MINUTES} minutes.', 'success')
    return redirect(url_for('portal_home'))


@app.route('/my/login/<token>')
def portal_login_link(token):
    if not _portal_on():
        abort(404)
    email = redeem_link_token(token)
    if not email:
        flash('That sign-in link has expired or was already used. Ask for a new one.', 'error')
        return redirect(url_for('portal_home'))
    sign_in(email)
    return redirect(_after_sign_in())


def _after_sign_in():
    device = session.pop('portal_next_device', None)
    return url_for('portal_report', device=device) if device else url_for('portal_home')


@app.route('/my/google')
def portal_google():
    if not _portal_on() or not feature_enabled('google_signin'):
        abort(404)
    try:
        return redirect(start(session, redirect_uri(request.url_root), purpose='portal'))
    except SigninError as e:
        flash(str(e), 'error')
        return redirect(url_for('portal_home'))


def finish_portal_google():
    """Called from the shared Google callback when the sign-in was for the portal."""
    next_device = session.get('portal_next_device')
    try:
        email = verify(session, request.args)
    except SigninError as e:
        flash(str(e), 'error')
        return redirect(url_for('portal_home'))
    from foxdesk.services.portal import known_email
    if not known_email(email):
        flash(f'{email} isn\'t on file with the school. Try the account the school gave you, or ask the office.', 'error')
        return redirect(url_for('portal_home'))
    sign_in(email)
    if next_device:
        session['portal_next_device'] = next_device
    return redirect(_after_sign_in())


@app.route('/my/logout')
def portal_logout():
    sign_out()
    flash('Signed out.', 'info')
    return redirect(url_for('portal_home'))


# ─── report a problem / tickets ───────────────────────────────────────────────

@app.route('/my/report', methods=['GET', 'POST'])
def portal_report():
    v = _require_viewer()
    device = request.args.get('device') or request.form.get('device') or ''
    if not v:
        if device:
            session['portal_next_device'] = device
        return redirect(url_for('portal_home'))
    person = v['person']
    if not person or not feature_enabled('tickets'):
        flash('Reporting a problem here is for students and staff.', 'error')
        return redirect(url_for('portal_home'))
    mine = devices_for(person)
    categories = TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all()
    if request.method == 'POST':
        from foxdesk.automation.engine import emit
        from foxdesk.services.attachments import _save_attachments
        from foxdesk.services.helpdesk import _create_ticket, _notify_ticket_requester
        category_id = request.form.get('category_id', type=int)
        subject = request.form.get('subject', '').strip()[:200]
        description = request.form.get('description', '').strip()
        if not (category_id and any(c.id == category_id for c in categories)) or not subject or not description:
            flash('Pick what it\'s about, and fill in a short title and what\'s happening.', 'error')
            return render_template('portal_report.html', v=v, devices=mine, categories=categories, device=device,
                                   form=request.form)
        asset_tag = device if device in {d[0] for d in mine} else (device if AssetRegistry.query.filter_by(asset_tag=device).first() else None)
        try:
            ticket = _create_ticket(category_id, subject, description, person=person, asset_tag=asset_tag,
                                    site_id=person.site_id)
            if feature_enabled('attachments'):
                _save_attachments('ticket', ticket.id, request.files.getlist('photos'), uploaded_by=person.full_name)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'error')
            return render_template('portal_report.html', v=v, devices=mine, categories=categories, device=device,
                                   form=request.form)
        emit('ticket.created', ticket)
        _notify_ticket_requester(ticket, 'ticket_received')
        flash(f'Thanks! Your request #{ticket.id} was sent to the help desk.', 'success')
        return redirect(url_for('portal_ticket', ticket_id=ticket.id))
    return render_template('portal_report.html', v=v, devices=mine, categories=categories, device=device, form={})


@app.route('/my/tickets/<int:ticket_id>', methods=['GET', 'POST'])
def portal_ticket(ticket_id):
    v = _require_viewer()
    if not v:
        return redirect(url_for('portal_home'))
    ticket = Ticket.query.get_or_404(ticket_id)
    if not may_see_ticket(v, ticket):
        abort(404)
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if body:
            db.session.add(TicketComment(ticket_id=ticket.id, body=body[:10000], from_requester=True,
                                         author_label=f'{v["person"].full_name} (portal)'))
            if ticket.status in ('resolved', 'closed'):
                ticket.status, ticket.resolved_at = 'open', None
            from datetime import datetime
            ticket.updated_at = datetime.utcnow()
            db.session.commit()
            flash('Sent to the help desk.', 'success')
        return redirect(url_for('portal_ticket', ticket_id=ticket.id))
    thread = [c for c in ticket.comments if c.from_requester or c.emailed_to_requester]
    return render_template('portal_ticket.html', v=v, ticket=ticket, thread=thread)


# ─── paying fees ──────────────────────────────────────────────────────────────

@app.route('/my/pay/<int:incident_id>', methods=['POST'])
def portal_pay(incident_id):
    v = _require_viewer()
    if not v:
        return redirect(url_for('portal_home'))
    incident = Incident.query.get_or_404(incident_id)
    if not may_pay(v, incident) or not _payments_on():
        abort(404)
    try:
        return redirect(start_checkout(incident, v['email'], _base_url()), code=303)
    except PaymentError as e:
        flash(str(e), 'error')
        return redirect(url_for('portal_home'))


@app.route('/my/paid')
def portal_paid():
    v = _require_viewer()
    if not v:
        return redirect(url_for('portal_home'))
    try:
        payment = confirm(request.args.get('session_id', ''))
    except PaymentError as e:
        flash(str(e), 'error')
        return redirect(url_for('portal_home'))
    if payment and payment.status == 'paid':
        flash(f'Payment received: ${payment.amount:.2f}. Thank you! A receipt is on its way from our payment service.', 'success')
    elif payment:
        flash('We haven\'t seen that payment go through yet. If you finished paying, it will show here shortly.', 'info')
    return redirect(url_for('portal_home'))


# ─── QR codes on device labels ────────────────────────────────────────────────

@app.route('/r/<path:asset_tag>')
def device_qr(asset_tag):
    """Where a device label's QR code points: staff go to the device; everyone
    else goes to Report a problem with this device (signing in first)."""
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_asset_assign', asset_tag=asset_tag))
    if _portal_on():
        return redirect(url_for('portal_report', device=asset_tag))
    return redirect(url_for('admin_login') + f'?next={quote("/admin/assets/" + asset_tag + "/assign")}')
