"""Payments: the dashboard, working on charges in bulk, and waiving fees."""
from datetime import timedelta
from decimal import Decimal

from conftest import A
from foxdesk.clock import utcnow


def fees(make):
    ana, bo = make.person('Ana', 'Ruiz'), make.person('Bo', 'Lee')
    make.device(tag='T-1', holder=ana)
    old = A.Incident(asset_tag='T-1', person_id=ana.id, person_name='Ana Ruiz', description='Cracked screen',
                     fee_charged=True, fee_amount=Decimal('40.00'), created_at=utcnow() - timedelta(days=100))
    new = A.Incident(asset_tag='T-1', person_id=ana.id, person_name='Ana Ruiz', description='Lost charger',
                     fee_charged=True, fee_amount=Decimal('15.00'))
    cat = make.ticket_category('Lost charger', price=Decimal('25.00'))
    t = A._create_ticket(cat.id, 'Charger', 'Lost it', person=bo)
    A.db.session.add_all([old, new])
    A.db.session.commit()
    return ana, bo, old, new, t


def test_waive_fee_when_opening_a_ticket(client, make):
    cat = make.ticket_category('Lost charger', price=Decimal('25.00'))
    form = client.get('/admin/tickets/new').get_data(as_text=True)
    assert 'data-fee="25.00"' in form and 'name="waive_fee"' in form
    client.post('/admin/tickets/new', data={'category_id': cat.id, 'subject': 'Waived one', 'description': 'd',
                                            'priority': 'normal', 'waive_fee': 'on'})
    client.post('/admin/tickets/new', data={'category_id': cat.id, 'subject': 'Charged one', 'description': 'd',
                                            'priority': 'normal'})
    waived = A.Ticket.query.filter_by(subject='Waived one').one().charges[0]
    charged = A.Ticket.query.filter_by(subject='Charged one').one().charges[0]
    assert waived.waived_at and waived.waive_reason == 'Waived when the ticket was opened' and not waived.is_owed
    assert charged.is_owed
    page = client.get(f'/admin/tickets/{waived.ticket_id}').get_data(as_text=True)
    assert 'Waived' in page and 'Undo waive' in page

    client.post(f'/admin/ticket_charges/{charged.id}/waive', data={'reason': 'Covered by the plan'})
    assert A.db.session.get(A.TicketCharge, charged.id).waive_reason == 'Covered by the plan'
    client.post(f'/admin/ticket_charges/{charged.id}/unwaive')
    assert A.db.session.get(A.TicketCharge, charged.id).is_owed


def test_dashboard_and_bulk_actions(client, make):
    ana, bo, old, new, t = fees(make)
    dash = client.get('/admin/payments').get_data(as_text=True)
    assert '$80.00' in dash and '3 charges · 2 people' in dash and 'Over 90 days' in dash
    page = client.get('/admin/payments/charges?status=owed&sort=amount').get_data(as_text=True)
    assert page.index('Cracked screen') < page.index('Lost charger — Charger'), 'largest first'
    assert 'Cracked screen' in client.get('/admin/payments/charges?age=90').get_data(as_text=True)
    assert 'Lost charger' not in client.get('/admin/payments/charges?age=90').get_data(as_text=True)

    r = client.post('/admin/payments/charges/bulk', data={'charge': [f'incident:{old.id}', f'ticket:{t.charges[0].id}'],
                                                          'action': 'paid', 'method': 'check', 'note': 'Check 1042'},
                    follow_redirects=True)
    assert b'Recorded 2 payment(s).' in r.data
    paid = A.db.session.get(A.Incident, old.id)
    assert (paid.paid_method, paid.paid_note) == ('check', 'Check 1042') and paid.paid_at
    r = client.post('/admin/payments/charges/bulk', data={'charge': [f'incident:{new.id}'], 'action': 'waive'}, follow_redirects=True)
    assert b'Say why' in r.data and A.db.session.get(A.Incident, new.id).is_owed
    r = client.post('/admin/payments/charges/bulk', data={'charge': [f'incident:{new.id}', f'incident:{old.id}'], 'action': 'waive',
                                                          'reason': 'Hardship'}, follow_redirects=True)
    assert b'Waived 1 charge(s). 1 skipped' in r.data
    assert A.db.session.get(A.Incident, new.id).waive_reason == 'Hardship'

    dash = client.get('/admin/payments').get_data(as_text=True)
    assert 'Nobody owes anything' in dash and '$65.00' in dash and 'Check' in dash
    assert 'Hardship' in client.get('/admin/payments/charges/export?status=all').get_data(as_text=True)
    assert 'Lost charger' not in client.get('/admin/fees?status=unpaid').get_data(as_text=True), 'waived isn\'t owed'
    client.post('/admin/payments/charges/bulk', data={'charge': [f'incident:{new.id}'], 'action': 'reopen'})
    assert A.db.session.get(A.Incident, new.id).is_owed


def test_waived_fee_isnt_payable_in_the_portal(make):
    _, _, _, new, _ = fees(make)
    new.waived_at = utcnow()
    A.db.session.commit()
    assert not A.unpaid_fee(new)
    assert A.Incident.query.filter_by(id=new.id).one().fee_status == 'waived'


def test_payments_tab(client, make):
    page = client.get('/admin').get_data(as_text=True)
    assert 'href="/admin/payments"' in page
    assert 'Charges' in client.get('/admin/payments').get_data(as_text=True)
