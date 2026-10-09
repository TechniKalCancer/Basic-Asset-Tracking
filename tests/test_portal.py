"""The portal (students/staff and parents), paying fees online, QR device links, and the refresh forecast."""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import g

from conftest import A, patch_everywhere


def turn_on(*keys):
    for k in keys:
        A.set_feature(k, True, 'test')
    A.db.session.commit()
    g.pop('_feature_overrides', None)


def link_from(sent_emails):
    return re.search(r'(/my/login/\S+)', sent_emails[-1][2]).group(1)


def sign_in(browser, email, sent_emails):
    browser.post('/my/login', data={'email': email})
    return browser.get(link_from(sent_emails), follow_redirects=True)


class FakeStripe:
    def __init__(self, paid_cents):
        self.paid_cents, self.calls = paid_cents, []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw.get('data')))
        if method == 'POST' and url.endswith('/checkout/sessions'):
            self.incident_id = kw['data']['metadata[incident_id]']
            return _Resp(200, {'id': 'cs_test_1', 'url': 'https://checkout.stripe.com/c/pay/cs_test_1'})
        if '/checkout/sessions/' in url:
            return _Resp(200, {'id': 'cs_test_1', 'payment_status': 'paid', 'status': 'complete',
                               'amount_total': self.paid_cents, 'metadata': {'incident_id': self.incident_id}})
        return _Resp(200, {})


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.content = status, payload, b'x'

    def json(self):
        return self._payload


def test_email_link_sign_in(anon_client, make, sent_emails):
    turn_on('portal')
    kid = make.person('Ana', 'Ruiz', email='ana@students.org')
    make.device(holder=kid)
    r = anon_client.post('/my/login', data={'email': 'nobody@else.org'}, follow_redirects=True)
    assert b'If nobody@else.org is on file' in r.data and not sent_emails, 'same answer, no email, for unknown addresses'

    page = sign_in(anon_client, 'ANA@students.org', sent_emails).get_data(as_text=True)
    assert 'Hi Ana' in page and 'Your devices' in page
    link = link_from(sent_emails)
    anon_client.get('/my/logout')
    r = anon_client.get(link, follow_redirects=True)
    assert b'expired or was already used' in r.data, 'links work once'

    anon_client.post('/my/login', data={'email': 'ana@students.org'})
    A.PortalLogin.query.update({'created_at': datetime.utcnow() - timedelta(minutes=30)})
    A.db.session.commit()
    assert b'expired' in anon_client.get(link_from(sent_emails), follow_redirects=True).data
    for _ in range(3):
        anon_client.post('/my/login', data={'email': 'ana@students.org'})
    assert A.PortalLogin.query.count() == 3, 'at most 3 links an hour per address'


def test_portal_session_is_not_an_admin_session(client, make, sent_emails):
    turn_on('portal')
    make.person('Ana', 'Ruiz', email='ana@students.org')
    sign_in(client, 'ana@students.org', sent_emails)
    r = client.get('/admin/registry')
    assert r.status_code == 302 and '/admin/login' in r.headers['Location'], 'signing in to the portal drops admin access'


def test_report_and_ticket_privacy(anon_client, make, sent_emails):
    turn_on('portal')
    cat = make.ticket_category('Hardware')
    kid = make.person('Ana', 'Ruiz', email='ana@students.org')
    other = make.person('Bo', 'Lee', email='bo@students.org')
    dev = make.device(holder=kid)

    r = anon_client.get(f'/r/{dev.asset_tag}')
    assert r.headers['Location'].endswith(f'/my/report?device={dev.asset_tag}')
    anon_client.get(r.headers['Location'])
    page = sign_in(anon_client, 'ana@students.org', sent_emails).get_data(as_text=True)
    assert f'{dev.asset_tag}' in page and 'Report a problem' in page

    anon_client.post('/my/report', data={'device': dev.asset_tag, 'category_id': cat.id, 'subject': 'Cracked screen',
                                         'description': 'Dropped it'})
    t = A.Ticket.query.one()
    assert (t.requester_person_id, t.asset_tag) == (kid.id, dev.asset_tag)
    A.db.session.add_all([A.TicketComment(ticket_id=t.id, body='INTERNAL NOTE', author_label='tech'),
                          A.TicketComment(ticket_id=t.id, body='We ordered a screen', author_label='tech', emailed_to_requester=True)])
    t.status = 'resolved'
    theirs = A.Ticket(category_id=cat.id, subject='Bo private', description='x', requester_person_id=other.id)
    A.db.session.add(theirs)
    A.db.session.commit()

    page = anon_client.get(f'/my/tickets/{t.id}').get_data(as_text=True)
    assert 'We ordered a screen' in page and 'INTERNAL NOTE' not in page
    anon_client.post(f'/my/tickets/{t.id}', data={'body': 'Thanks!'})
    t = A.Ticket.query.get(t.id)
    assert t.status == 'open' and A.TicketComment.query.filter_by(ticket_id=t.id, from_requester=True).one().body == 'Thanks!'
    assert anon_client.get(f'/my/tickets/{theirs.id}').status_code == 404


def test_parent_sees_child_and_pays_online(client, anon_client, make, sent_emails, monkeypatch):
    turn_on('parent_portal')
    kid = make.person('Ana', 'Ruiz', email='ana@students.org', guardian_email='Mom@Home.org')
    stranger = make.person('Bo', 'Lee', email='bo@students.org')
    dev = make.device(holder=kid)
    fee = A.Incident(asset_tag=dev.asset_tag, person_id=kid.id, person_name=kid.full_name, description='Cracked screen',
                     fee_charged=True, fee_amount=Decimal('45.00'))
    other_fee = A.Incident(asset_tag='X1', person_id=stranger.id, description='x', fee_charged=True, fee_amount=Decimal('10'))
    A.db.session.add_all([fee, other_fee])
    A.db.session.commit()

    page = sign_in(anon_client, 'mom@home.org', sent_emails).get_data(as_text=True)
    assert 'Ana Ruiz' in page and dev.asset_tag in page and '$45.00 due' in page, 'no Stripe key yet: pay at the office'

    r = client.post('/admin/portal', data={'stripe_secret_key': 'not-a-key'}, follow_redirects=True)
    assert b'Stripe secret key' in r.data
    client.post('/admin/portal', data={'stripe_secret_key': 'sk_test_notreal', 'payment_note': 'Pay at the office'})
    g.pop('_feature_overrides', None)
    assert A.feature_enabled('online_payments') and 'sk_test_notreal' not in A.PortalSettings.query.get(1).stripe_secret_key

    stripe = FakeStripe(paid_cents=4500)
    patch_everywhere(monkeypatch, 'requests', type('R', (), {'request': staticmethod(stripe),
                                                             'RequestException': Exception}))
    assert anon_client.post(f'/my/pay/{other_fee.id}').status_code == 404, 'only your own student\'s fees'
    r = anon_client.post(f'/my/pay/{fee.id}')
    assert r.status_code == 303 and r.headers['Location'].startswith('https://checkout.stripe.com/')
    sent = stripe.calls[0][2]
    assert sent['line_items[0][price_data][unit_amount]'] == 4500 and sent['customer_email'] == 'mom@home.org'
    r = anon_client.get('/my/paid?session_id=cs_test_1', follow_redirects=True)
    assert b'Payment received: $45.00' in r.data
    assert A.Incident.query.get(fee.id).paid_at and A.FeePayment.query.one().status == 'paid'
    assert A.ActivityLog.query.filter_by(action='fee_paid').count() == 1


def test_wrong_amount_is_never_marked_paid(client, anon_client, make, sent_emails, monkeypatch):
    turn_on('parent_portal')
    kid = make.person('Ana', 'Ruiz', email='ana@students.org', guardian_email='mom@home.org')
    fee = A.Incident(asset_tag='T1', person_id=kid.id, description='x', fee_charged=True, fee_amount=Decimal('45.00'))
    A.db.session.add(fee)
    A.db.session.commit()
    client.post('/admin/portal', data={'stripe_secret_key': 'sk_test_notreal'})
    g.pop('_feature_overrides', None)
    stripe = FakeStripe(paid_cents=100)
    patch_everywhere(monkeypatch, 'requests', type('R', (), {'request': staticmethod(stripe), 'RequestException': Exception}))
    sign_in(anon_client, 'mom@home.org', sent_emails)
    anon_client.post(f'/my/pay/{fee.id}')
    A.reconcile_pending()
    assert A.FeePayment.query.one().status == 'review' and A.Incident.query.get(fee.id).paid_at is None


def test_portal_google_sign_in(client, anon_client, make, monkeypatch):
    turn_on('portal')
    make.person('Ana', 'Ruiz', email='ana@students.org')
    client.post('/admin/signin/google', data={'client_id': 'x.apps.googleusercontent.com', 'client_secret': 'GOCSPX-x',
                                              'domains': 'fchs.net'})
    g.pop('_feature_overrides', None)
    r = anon_client.get('/my/google')
    assert 'hd=' not in r.headers['Location'], 'students aren\'t limited to the staff domain hint'
    with anon_client.session_transaction() as s:
        pending = s['oauth']
    patch_everywhere(monkeypatch, '_exchange_code', lambda data: _Resp(200, {'id_token': 'jwt'}))
    patch_everywhere(monkeypatch, '_verify_id_token', lambda token, cid: {'email': 'ana@students.org', 'email_verified': True,
                                                                          'nonce': pending['nonce']})
    page = anon_client.get(f'/auth/google/callback?code=c&state={pending["state"]}', follow_redirects=True).get_data(as_text=True)
    assert 'Hi Ana' in page


def test_portal_off_by_default(anon_client):
    assert anon_client.get('/my').status_code == 404


def test_qr_labels_and_device_links(client, make):
    dev = make.device()
    r = client.post('/admin/labels/avery', data={'template': '5160', 'asset_tags': dev.asset_tag, 'qr': 'on'})
    assert b'class="qr"' in r.data
    assert client.get(f'/r/{dev.asset_tag}').headers['Location'].endswith(f'/admin/assets/{dev.asset_tag}/assign')


def test_refresh_forecast(client, make):
    assert A.parse_aue({'autoUpdateThrough': '2027-06-30T00:00:00Z'}) == date(2027, 6, 30)
    assert A.parse_aue({'autoUpdateExpiration': '1814400000000'}) == date(2027, 7, 1)
    assert A.parse_aue({}) is None
    today = date.today()
    old, soon, later, gone = make.device(), make.device(), make.device(), make.device(status='retired')
    for row, aue in ((old, today - timedelta(days=30)), (soon, today + timedelta(days=20)),
                     (later, today + timedelta(days=800)), (gone, today - timedelta(days=30))):
        A.Asset.query.filter_by(asset_tag=row.asset_tag).one().google_aue_date = aue
    A.db.session.commit()
    f = A._refresh_forecast(None)
    assert [p[0].asset_tag for p in f['past_in_use']] == [old.asset_tag], 'retired devices aren\'t counted'
    assert sum(y['count'] for y in f['years']) == 2
    page = client.get('/admin/refresh_forecast').get_data(as_text=True)
    assert old.asset_tag in page and 'Refresh forecast' in page
    csv = client.get('/admin/refresh_forecast?format=csv').get_data(as_text=True)
    assert csv.splitlines()[0].startswith('asset_tag,') and old.asset_tag in csv
