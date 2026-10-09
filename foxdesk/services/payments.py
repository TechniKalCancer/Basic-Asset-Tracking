"""Online damage-fee payments through Stripe Checkout.

A parent or student clicks Pay, FoxDesk creates a Checkout Session for
exactly the fee on record and sends them to Stripe's payment page. A fee is
only marked paid after FoxDesk asks Stripe itself and Stripe confirms the
session is paid, for the same amount, for the same damage report — on the
return from Stripe, and again from a background check every 15 minutes for
anyone who paid and closed the tab. No webhook (and so no public endpoint)
is needed. Card details never touch FoxDesk.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import requests

from foxdesk.core import db, logger
from foxdesk.models import FeePayment, Incident, PortalSettings
from foxdesk.services.secrets import decrypt_secret, encrypt_secret

STRIPE = 'https://api.stripe.com/v1'
SECRET_PURPOSE = 'stripe-secret-key'
CURRENCY = 'usd'


class PaymentError(Exception):
    """Shown to the person paying or the admin."""


def portal_settings():
    row = PortalSettings.query.get(1)
    if row is None:
        row = PortalSettings(id=1)
        db.session.add(row)
        db.session.flush()
    return row


def stripe_key():
    row = PortalSettings.query.get(1)
    return decrypt_secret(row.stripe_secret_key, SECRET_PURPOSE) if row and row.stripe_secret_key else None


def save_stripe_key(key):
    key = (key or '').strip()
    if key and not key.startswith(('sk_live_', 'sk_test_', 'rk_live_', 'rk_test_')):
        raise ValueError('That doesn\'t look like a Stripe secret key (it starts with sk_live_ or sk_test_).')
    if key:
        portal_settings().stripe_secret_key = encrypt_secret(key, SECRET_PURPOSE)


def is_test_mode():
    key = stripe_key() or ''
    return '_test_' in key


def cents(amount):
    return int((Decimal(amount) * 100).quantize(Decimal('1')))


def _stripe(method, path, **kwargs):
    key = stripe_key()
    if not key:
        raise PaymentError('Online payment isn\'t set up.')
    try:
        response = requests.request(method, f'{STRIPE}{path}', auth=(key, ''), timeout=20, **kwargs)
    except requests.RequestException:
        raise PaymentError('Couldn\'t reach the payment service. Please try again in a minute.')
    if response.status_code == 401:
        raise PaymentError('The payment service rejected FoxDesk\'s key. Tell the school office.')
    if response.status_code >= 400:
        message = ((response.json() or {}).get('error') or {}).get('message') if response.content else None
        raise PaymentError(f'The payment service said: {message or response.status_code}')
    return response.json()


def check_key():
    """Raises PaymentError unless Stripe accepts the saved key."""
    _stripe('GET', '/balance')


def unpaid_fee(incident):
    return bool(incident.fee_charged and incident.fee_amount and Decimal(incident.fee_amount) > 0 and not incident.paid_at)


def start_checkout(incident, payer_email, base_url):
    """A Stripe Checkout URL for this fee; records the pending payment. Commits."""
    if not unpaid_fee(incident):
        raise PaymentError('That fee is already paid.')
    session = _stripe('POST', '/checkout/sessions', data={
        'mode': 'payment',
        'success_url': f'{base_url}/my/paid?session_id={{CHECKOUT_SESSION_ID}}',
        'cancel_url': f'{base_url}/my',
        'customer_email': payer_email or None,
        'client_reference_id': str(incident.id),
        'metadata[incident_id]': str(incident.id),
        'line_items[0][quantity]': 1,
        'line_items[0][price_data][currency]': CURRENCY,
        'line_items[0][price_data][unit_amount]': cents(incident.fee_amount),
        'line_items[0][price_data][product_data][name]': f'Device damage fee ({incident.asset_tag})',
        'line_items[0][price_data][product_data][description]': (incident.description or 'Damage fee')[:200],
    })
    db.session.add(FeePayment(incident_id=incident.id, amount=Decimal(incident.fee_amount), provider='stripe',
                              provider_ref=session['id'], status='pending', payer_email=payer_email))
    db.session.commit()
    return session['url']


def confirm(session_id):
    """Ask Stripe about one checkout session and settle it. Returns the
    FeePayment (None if it isn't ours). Commits."""
    from foxdesk.services.auth import _log_activity
    payment = FeePayment.query.filter_by(provider_ref=session_id).first()
    if payment is None or payment.status == 'paid':
        return payment
    data = _stripe('GET', f'/checkout/sessions/{session_id}')
    incident = Incident.query.get(payment.incident_id)
    matches = (data.get('payment_status') == 'paid' and data.get('amount_total') == cents(payment.amount)
               and str((data.get('metadata') or {}).get('incident_id')) == str(payment.incident_id))
    if matches:
        payment.status, payment.paid_at = 'paid', datetime.utcnow()
        if incident and not incident.paid_at:
            incident.paid_at = payment.paid_at
            _log_activity('fee_paid', f'Damage fee for {incident.asset_tag} ({incident.person_name or "unknown"}) '
                                      f'paid online: ${payment.amount:.2f} (Stripe {session_id[-8:]}).')
    elif data.get('status') == 'expired':
        payment.status = 'expired'
    elif data.get('payment_status') == 'paid':
        payment.status = 'review'  # paid, but not what FoxDesk asked for — leave it to a person
        logger.warning('Stripe session %s paid but did not match fee %s', session_id, payment.incident_id)
    db.session.commit()
    return payment


def reconcile_pending():
    """Settle payments still pending from the last two days (someone paid and
    closed the tab before coming back)."""
    if not stripe_key():
        return 0
    settled = 0
    for p in FeePayment.query.filter(FeePayment.status == 'pending',
                                     FeePayment.created_at >= datetime.utcnow() - timedelta(days=2)):
        try:
            if confirm(p.provider_ref).status == 'paid':
                settled += 1
        except PaymentError as e:
            logger.warning('Could not check payment %s: %s', p.provider_ref, e)
            break
    return settled
