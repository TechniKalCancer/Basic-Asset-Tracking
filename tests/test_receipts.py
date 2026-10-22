"""Receipts for every payment, and printable invoices for home."""
from decimal import Decimal

from conftest import A


def fee(make, person, amount, description='Cracked screen'):
    row = make.device(holder=person)
    inc = A.Incident(asset_tag=row.asset_tag, person_id=person.id, person_name=person.full_name, description=description,
                     fee_charged=True, fee_amount=Decimal(amount))
    A.db.session.add(inc)
    A.db.session.commit()
    return inc


def ticket_charge(make, person, amount, what='Keyboard'):
    cat = A.TicketCategory.query.filter_by(name='Hardware').first() or make.ticket_category()
    t = A.Ticket(category_id=cat.id, subject='Broken keys', description='x', requester_person_id=person.id,
                 requester_name=person.full_name, requester_email=person.email)
    A.db.session.add(t)
    A.db.session.flush()
    c = A.TicketCharge(ticket_id=t.id, description=what, amount=Decimal(amount))
    A.db.session.add(c)
    A.db.session.commit()
    return c


def test_bulk_payment_makes_one_receipt_per_person(client, make):
    ana = make.person('Ana', 'Ruiz', guardian_name='Maria Ruiz', external_id='100234', grad_year=2030)
    bo = make.person('Bo', 'Lee')
    a1, a2, b1 = fee(make, ana, '40'), ticket_charge(make, ana, '15'), fee(make, bo, '25', 'Lost charger')
    r = client.post('/admin/payments/charges/bulk', data={'charge': [f'incident:{a1.id}', f'ticket:{a2.id}', f'incident:{b1.id}'],
                                                          'action': 'paid', 'method': 'check', 'note': 'Check 1042'},
                    follow_redirects=True)
    page = r.get_data(as_text=True)
    assert 'Recorded 3 payment(s), with 2 receipt(s).' in page and '2 receipts made.' in page
    receipts = sorted(A.Receipt.query.all(), key=lambda x: x.person_name)
    assert [(x.person_name, x.amount) for x in receipts] == [('Ana Ruiz', Decimal('55.00')), ('Bo Lee', Decimal('25.00'))]
    assert A.db.session.get(A.Incident, a1.id).receipt_id == A.db.session.get(A.TicketCharge, a2.id).receipt_id == receipts[0].id
    assert receipts[0].number == f'R-{receipts[0].id:06d}' and len(receipts[0].items) == 2

    printed = client.get(f'/admin/receipts/print?id={receipts[0].id}&id={receipts[1].id}').get_data(as_text=True)
    assert printed.count('class="pp-doc"') == 2 and 'Parent/guardian of Ana Ruiz' in printed and 'Maria Ruiz' in printed
    assert 'Check · Check 1042' in printed and '$55.00' in printed and 'Student ID 100234' in printed
    assert 'Nothing else is owed' in printed
    listing = client.get('/admin/receipts?q=' + receipts[1].number).get_data(as_text=True)
    assert 'Bo Lee' in listing and 'Ana Ruiz' not in listing
    assert receipts[0].number in client.get('/admin/payments/charges?status=paid').get_data(as_text=True)


def test_single_payments_each_make_a_receipt(client, make):
    ana = make.person('Ana', 'Ruiz')
    inc, charge = fee(make, ana, '30'), ticket_charge(make, ana, '12')
    r = client.post(f'/admin/incidents/{inc.id}/mark_paid', data={'method': 'cash'}, follow_redirects=True)
    assert b'Receipt R-' in r.data
    inc = A.db.session.get(A.Incident, inc.id)
    assert inc.paid_method == 'cash' and A.db.session.get(A.Receipt, inc.receipt_id).method == 'cash'
    assert f'/admin/receipts/{inc.receipt_id}' in client.get(f'/admin/assets/{inc.asset_tag}/assign').get_data(as_text=True)

    client.post(f'/admin/ticket_charges/{charge.id}/mark_paid', data={'method': 'card', 'note': 'Visa 4421'})
    charge = A.db.session.get(A.TicketCharge, charge.id)
    assert charge.receipt_id and A.db.session.get(A.Receipt, charge.receipt_id).reference == 'Visa 4421'
    again = client.post(f'/admin/ticket_charges/{charge.id}/mark_paid', data={'method': 'cash'}, follow_redirects=True)
    assert b'already settled' in again.data and A.Receipt.query.count() == 2, 'paying twice makes no second receipt'

    person_page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert person_page.count('/admin/receipts/') >= 2 and 'Print invoice' not in person_page, 'nothing owed now'


def test_plan_payment_receipt(client, make):
    from test_person_page import agreement
    kid = make.person('Ana', 'Ruiz')
    ag = agreement(client, 'Device agreement')
    ag.plan_offered, ag.plan_name, ag.plan_price = True, 'Protection plan', Decimal('20')
    A.db.session.commit()
    client.post(f'/admin/agreements/{ag.id}/paper/{kid.id}', data={'signer_name': 'Maria Ruiz', 'plan_choice': 'enrolled'})
    sig = A.AgreementSignature.query.filter_by(plan_choice='enrolled').one()
    client.post(f'/admin/agreement_signatures/{sig.id}/plan_paid', data={'method': 'cash'})
    sig = A.db.session.get(A.AgreementSignature, sig.id)
    assert sig.plan_paid_at and sig.plan_receipt_id
    assert A.db.session.get(A.Receipt, sig.plan_receipt_id).items[0]['kind'] == 'Protection plan'


def test_invoice_for_home(client, make):
    client.post('/admin/receipts/settings', data={'payable_to': 'Fox Creek School', 'contact': '123 Main St\nNorth Augusta, SC',
                                                  'pay_instructions': '', 'footer': 'Questions? Call the front office.'})
    site = make.site('Fox Creek Middle School')
    ana = make.person('Ana', 'Ruiz', site=site, guardian_name='Maria Ruiz', guardian_email='maria@home.net', external_id='100234')
    owed, paid = fee(make, ana, '40'), fee(make, ana, '10', 'Sticker removal')
    ticket_charge(make, ana, '15')
    client.post(f'/admin/incidents/{paid.id}/mark_paid', data={'method': 'cash'})

    page = client.get(f'/admin/people/{ana.id}/invoice').get_data(as_text=True)
    assert 'INVOICE' in page and 'Parent/guardian of Ana Ruiz' in page and 'maria@home.net' in page
    assert 'Fox Creek Middle School' in page and '123 Main St' in page and 'Questions? Call the front office.' in page
    assert '$55.00' in page and owed.description in page and 'Sticker removal' not in page.split('Amount due')[0]
    assert 'Make checks payable to <strong>Fox Creek School</strong>' in page and 'Return this part with your payment' in page
    assert 'Already paid this school year: $10.00' in page
    assert 'Print invoice' in client.get(f'/admin/people/{ana.id}').get_data(as_text=True)

    bo = make.person('Bo', 'Lee')
    fee(make, bo, '25')
    make.person('Cy', 'Clear')
    many = client.get('/admin/invoices/print?owing=1').get_data(as_text=True)
    assert many.count('class="pp-doc"') == 2 and 'Bo Lee' in many and 'Cy Clear' not in many
    r = client.post('/admin/payments/charges/bulk', data={'charge': [f'incident:{owed.id}'], 'action': 'invoices'})
    assert r.status_code == 302 and f'person={ana.id}' in r.headers['Location']
    nothing = client.get(f'/admin/invoices/print?person={make.person("Di", "Done").id}', follow_redirects=True)
    assert b'Nobody selected owes anything' in nothing.data


def test_receipts_for_earlier_payments(client, make):
    from datetime import timedelta
    from foxdesk.clock import utcnow
    ana = make.person('Ana', 'Ruiz')
    old = fee(make, ana, '35')
    old.paid_at, old.paid_method = utcnow() - timedelta(days=40), 'check'
    A.db.session.commit()
    page = client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    assert 'Make receipts for 1 earlier payment' in page
    r = client.post(f'/admin/people/{ana.id}/receipts_for_earlier')
    assert r.status_code == 302 and '/admin/receipts/print' in r.headers['Location']
    receipt = A.Receipt.query.one()
    assert receipt.method == 'check' and receipt.paid_at.date() == old.paid_at.date()
    assert 'Make receipts for' not in client.get(f'/admin/people/{ana.id}').get_data(as_text=True)
    r = client.post(f'/admin/people/{ana.id}/receipts_for_earlier', follow_redirects=True)
    assert b'Every payment already has a receipt' in r.data
