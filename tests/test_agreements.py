"""Device agreements: signing in the portal, paper copies, the protection plan, reminders."""
from decimal import Decimal

from conftest import A, patch_everywhere
from test_portal import sign_in, turn_on

TEXT = 'I will take care of my school device.\n\nI will charge it every night and keep it in its case.'


def make_agreement(client, **extra):
    form = {'title': 'Device agreement', 'school_year': '2026-27', 'body': TEXT,
            'require_student': 'on', 'require_guardian': 'on'}
    form.update(extra)
    r = client.post('/admin/agreements/new', data=form, follow_redirects=True)
    assert b'is open for signing' in r.data, r.data[:2000]
    return A.Agreement.query.order_by(A.Agreement.id.desc()).first()


def family(make):
    kid = make.person('Ana', 'Ruiz', email='ana@students.org', guardian_name='Maria Ruiz', guardian_email='maria@home.net')
    other = make.person('Bo', 'Lee', email='bo@students.org', guardian_email='lee@home.net')
    return kid, other


class FakeStripe:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, **kw):
        from test_portal import _Resp
        self.calls.append((method, url, kw.get('data')))
        if method == 'POST':
            self.sig = kw['data']['metadata[signature_id]']
            self.cents = kw['data']['line_items[0][price_data][unit_amount]']
            return _Resp(200, {'id': 'cs_test_plan', 'url': 'https://checkout.stripe.com/c/pay/cs_test_plan'})
        return _Resp(200, {'id': 'cs_test_plan', 'payment_status': 'paid', 'status': 'complete',
                           'amount_total': self.cents, 'metadata': {'signature_id': self.sig}})


def test_family_signs_in_the_portal_and_pays_for_the_plan(client, make, sent_emails, monkeypatch):
    turn_on('portal', 'parent_portal')
    kid, other = family(make)
    agreement = make_agreement(client, plan_offered='on', plan_name='Protection plan', plan_price='25.00')
    browser = A.app.test_client()

    home = sign_in(browser, 'ana@students.org', sent_emails).get_data(as_text=True)
    assert 'Needs your attention' in home and 'Read and sign' in home
    url = f'/my/agreements/{agreement.id}/{kid.id}'
    page = browser.get(url).get_data(as_text=True)
    assert 'charge it every night' in page and 'Sign as the student' in page and 'Yes, enroll' not in page, \
        'the parent picks the plan, not the student'
    r = browser.post(url, data={'signer_name': 'Ana Ruiz'}, follow_redirects=True)
    assert b'Check the box' in r.data and A.AgreementSignature.query.count() == 0
    browser.post(url, data={'signer_name': 'Ana Ruiz', 'agree': 'on'})
    sig = A.AgreementSignature.query.one()
    assert (sig.signer_role, sig.signer_email, sig.body_sha256) == ('student', 'ana@students.org', A.body_hash(agreement))
    assert browser.get(f'/my/agreements/{agreement.id}/{other.id}').status_code == 404, 'only your own students'

    parent = A.app.test_client()
    home = sign_in(parent, 'maria@home.net', sent_emails).get_data(as_text=True)
    assert 'Read and sign' in home
    r = parent.post(url, data={'signer_name': 'Maria Ruiz', 'agree': 'on'}, follow_redirects=True)
    assert b'Choose whether to enroll' in r.data
    r = parent.post(url, data={'signer_name': 'Maria Ruiz', 'agree': 'on', 'plan_choice': 'enrolled'}, follow_redirects=True)
    assert b'Your plan isn&#39;t paid yet' in r.data
    st = A.status_for(agreement, kid)
    assert st['complete'] and st['plan_unpaid'] and not A.db.session.get(A.Person, kid.id).insurance_opted_in

    patch_everywhere(monkeypatch, 'stripe_key', lambda: 'sk_test_x')
    stripe = FakeStripe()
    monkeypatch.setattr(A.requests, 'request', stripe)
    turn_on('online_payments')
    r = parent.post(f'/my/agreements/pay/{st["plan"].id}')
    assert r.status_code == 303 and stripe.cents == 2500
    parent.get('/my/paid?session_id=cs_test_plan')
    assert A.db.session.get(A.Person, kid.id).insurance_opted_in, 'paying turns on the protection plan'
    assert A.FeePayment.query.one().status == 'paid' and not A.status_for(agreement, kid)['plan_unpaid']
    assert 'Protection plan' in client.get('/admin/portal').get_data(as_text=True)


def test_paper_copy_plan_paid_and_staff_pages(client, make):
    kid, other = family(make)
    agreement = make_agreement(client, plan_offered='on', plan_name='Protection plan', plan_price='20')
    page = client.get(f'/admin/agreements/{agreement.id}').get_data(as_text=True)
    assert 'Not signed' in page and 'Record signed paper copy' in page
    assert 'Device agreement not signed' in client.get(f'/admin/assets/{make.device(holder=kid).asset_tag}/assign').get_data(as_text=True)

    client.post(f'/admin/agreements/{agreement.id}/paper/{kid.id}', data={'signer_name': 'Maria Ruiz', 'plan_choice': 'enrolled'})
    st = A.status_for(agreement, kid)
    assert st['complete'] and st['plan_unpaid'], 'a paper copy covers every signature'
    client.post(f'/admin/agreement_signatures/{st["plan"].id}/plan_paid')
    assert A.db.session.get(A.Person, kid.id).insurance_opted_in
    client.post(f'/admin/agreements/{agreement.id}/paper/{other.id}', data={'signer_name': 'Ki Lee', 'plan_choice': 'declined'})

    page = client.get(f'/admin/agreements/{agreement.id}?show=complete').get_data(as_text=True)
    assert 'Ana Ruiz' in page and 'Bo Lee' in page
    csv_text = client.get(f'/admin/agreements/{agreement.id}/export').get_data(as_text=True)
    assert 'Maria Ruiz (Paper copy' in csv_text and 'declined' in csv_text
    assert 'Device agreements' in client.get(f'/admin/people/{kid.id}/history').get_data(as_text=True)
    assert '2 of 2 students signed' in client.get('/admin/agreements').get_data(as_text=True)


def test_signed_text_is_locked_and_new_versions(client, make):
    kid, _ = family(make)
    agreement = make_agreement(client)
    client.post(f'/admin/agreements/{agreement.id}/paper/{kid.id}', data={'signer_name': 'Maria Ruiz'})
    client.post(f'/admin/agreements/{agreement.id}/edit', data={'title': 'Renamed', 'body': 'Something else entirely, new terms.'})
    agreement = A.db.session.get(A.Agreement, agreement.id)
    assert agreement.title == 'Renamed' and agreement.body == TEXT, 'the signed text never changes'
    r = client.post(f'/admin/agreements/{agreement.id}/new_version', follow_redirects=True)
    assert b'New version made' in r.data
    old, new = A.Agreement.query.order_by(A.Agreement.id).all()
    assert (old.is_active, new.is_active, new.body) == (False, True, TEXT)
    assert not A.status_for(new, kid)['complete'], 'everyone signs the new version'


def test_reminders_and_automation_field(client, make, sent_emails):
    kid, other = family(make)
    agreement = make_agreement(client)
    page = client.get(f'/admin/agreements/{agreement.id}').get_data(as_text=True)
    assert 'Turn on the My stuff or parent portal' in page
    turn_on('portal', 'parent_portal')
    client.post(f'/admin/agreements/{agreement.id}/paper/{other.id}', data={'signer_name': 'Ki Lee'})
    sent_emails.clear()
    r = client.post(f'/admin/agreements/{agreement.id}/remind', follow_redirects=True)
    assert b'Reminders emailed to 2 address(es)' in r.data
    assert sorted(to for to, _, _ in sent_emails) == ['ana@students.org', 'maria@home.net']
    assert '/my' in sent_emails[0][2]
    r = client.post(f'/admin/agreements/{agreement.id}/remind', follow_redirects=True)
    assert b'already went out' in r.data and len(sent_emails) == 2

    assert A.agreements_complete(A.db.session.get(A.Person, other.id))
    assert not A.agreements_complete(A.db.session.get(A.Person, kid.id))
    staff = make.person('Pat', 'Teach', role='staff')
    assert A.agreements_complete(staff), 'agreements are for students'
    assert 'unsigned_agreement_email' in A.TEMPLATES_BY_KEY


def test_agreement_form_needs_text_and_signers(client):
    r = client.post('/admin/agreements/new', data={'title': 'X', 'body': 'short'}, follow_redirects=True)
    assert b'Paste in the agreement text' in r.data and b'Choose who signs' in r.data
    assert A.Agreement.query.count() == 0
